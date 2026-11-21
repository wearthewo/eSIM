"""Screenshot every page at desktop and phone width, for reviewing the UI by eye.

Usage (from the repo root; needs TEST_DATABASE_URL in .env and the dev extras):
    .venv\\Scripts\\python.exe -m pip install -e .[dev]
    .venv\\Scripts\\python.exe -m playwright install chromium
    .venv\\Scripts\\python.exe scripts\\screens.py
    .venv\\Scripts\\python.exe scripts\\screens.py --only login,upload --skip-generate

Writes ``output/screens/<page>-<desktop|phone>[-dark].png`` (``output/`` is gitignored).

The app runs on 127.0.0.1 against the TEST database, which this script empties and
fills with synthetic accounts (``*.test`` emails) at the start of every pass, exactly
as the test suite does. It refuses to run if TEST_DATABASE_URL is the DATABASE_URL
database. Uploads are the synthetic files in ``tests/fixtures`` or synthetic CSVs
built in memory by ``make_big_csv.make_csv``. Downloads are refused by the browser,
so no ZIP or PDF is written anywhere. The folder-mode shots (``upload-folder-*``) use
an in-memory stand-in for the folder picker that drops every byte (headless Chromium
cannot show the picker). Keys are random per run, never the .env ones.

Two-factor authentication: the seeded superadmin has it on (with a random secret), so
the admin shots use a direct session as before. A member with a known synthetic
password and 2FA shows the second login step (``login-2fa*``); the owner sets 2FA up
during the run (``account-2fa-*``, the code is computed from the key on the page); a
second superadmin without 2FA shows the forced enrolment (``admin-2fa-required``).
"""

from __future__ import annotations

import argparse
import base64
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from make_big_csv import make_csv  # noqa: E402
from playwright.sync_api import BrowserContext, Page, sync_playwright  # noqa: E402
from sqlalchemy import Engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import batches  # noqa: E402
from app.auth import accounts, mfa  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.core.pipeline import check_upload, part_count, part_records  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.engine import make_engine  # noqa: E402
from app.db.models import (  # noqa: E402
    AccessRequest,
    AccessRequestStatus,
    Batch,
    BatchStatus,
    Organization,
    Role,
    Usage,
    User,
    VolumeBand,
)
from app.settings import Secret, Settings, get_settings  # noqa: E402
from app.web.auth import SESSION_COOKIE  # noqa: E402
from app.web.main import create_app  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
OUTPUT = ROOT / "output" / "screens"
VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
ADMIN_EMAIL = "admin@platform.test"
OWNER_EMAIL = "owner@demo-mobile.test"
WRONG_PASSWORD = "not the password"  # noqa: S105  (synthetic, for the login error state)
MFA_EMAIL = "secure@demo-mobile.test"
MFA_PASSWORD = "synthetic screens passphrase"  # noqa: S105  (a synthetic test account)
NEW_ADMIN_EMAIL = "new.admin@platform.test"
MULTI_PART_ROWS = 1_200  # three parts of 500: shows the progress bar part-way
CONTINUE_ROWS = 1_200  # the unfinished batch: part 1 of 3 downloaded


def continue_csv() -> bytes:
    """The unfinished batch's file (other rows than the multi-part shots use)."""
    return make_csv(CONTINUE_ROWS, first_key=90_001)


# The parts list (download parts one by one): as in a browser without the File System
# Access API.
NO_FOLDER_API = """
delete window.showDirectoryPicker;
if (typeof window.showDirectoryPicker === "function") window.showDirectoryPicker = undefined;
"""
# Folder mode: a folder that exists only in memory and drops what is written to it.
FAKE_FOLDER = """
(() => {
  const names = new Set();
  const folder = {
    kind: "directory",
    name: "eSIM orders",
    async getFileHandle(name, options) {
      if (!names.has(name)) {
        if (!(options && options.create)) throw new DOMException("not found", "NotFoundError");
        names.add(name);
      }
      return { kind: "file", name, async createWritable() { return new WritableStream(); } };
    },
    async removeEntry(name) { names.delete(name); },
  };
  window.showDirectoryPicker = async () => folder;
})();
"""


# --- Database ------------------------------------------------------------------------------


def test_engine() -> Engine:
    settings = get_settings()
    if settings.test_database_url is None:
        sys.exit("TEST_DATABASE_URL is not set (see README, Database).")
    test_url = make_url(settings.test_database_url.reveal())
    if settings.database_url is not None:
        dev_url = make_url(settings.database_url.reveal())
        if (test_url.host, test_url.port, test_url.database) == (
            dev_url.host,
            dev_url.port,
            dev_url.database,
        ):
            sys.exit("TEST_DATABASE_URL points at the DATABASE_URL database; refusing to run.")
    engine = make_engine(settings.test_database_url.reveal())
    with engine.begin() as conn:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.attributes["connection"] = conn
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")
    return engine


@dataclass
class Seeded:
    org_id: int
    admin_token: str
    owner_token: str
    new_admin_token: str  # a superadmin without 2FA yet


def seed(
    engine: Engine, sessions: sessionmaker[Session], key: bytes, mfa_keys: mfa.MfaKeys
) -> Seeded:
    """Empty the test database and add a small synthetic world."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    now = datetime.now(UTC)
    unusable = hash_password(secrets.token_urlsafe(32))  # a real hash of a random secret
    with sessions.begin() as db:
        org = Organization(name="Demo Mobile (synthetic)")
        other = Organization(name="Northwind Telecom (synthetic)", daily_row_quota=50_000)
        paused = Organization(name="Paused Networks (synthetic)", disabled=True)
        db.add_all([org, other, paused])
        db.flush()
        admin = User(
            email=ADMIN_EMAIL,
            is_superadmin=True,
            role=Role.MEMBER,
            password_hash=unusable,
            last_login_at=now,
        )
        owner = User(
            email=OWNER_EMAIL,
            organization_id=org.id,
            role=Role.OWNER,
            password_hash=unusable,  # sessions are created directly, never by logging in
            last_login_at=now - timedelta(hours=2),
        )
        db.add_all(
            [
                admin,
                owner,
                User(
                    email="ops@demo-mobile.test",
                    organization_id=org.id,
                    role=Role.MEMBER,
                    password_hash=unusable,
                    last_login_at=now - timedelta(days=3),
                ),
                User(email="new.hire@demo-mobile.test", organization_id=org.id),
                User(
                    email="former@demo-mobile.test",
                    organization_id=org.id,
                    password_hash=unusable,
                    disabled=True,
                    last_login_at=now - timedelta(days=40),
                ),
                User(email="owner@northwind.test", organization_id=other.id, role=Role.OWNER),
            ]
        )
        db.flush()
        db.add_all(
            [
                Usage(organization_id=org.id, day=now.date(), rows_generated=1_234),
                Usage(organization_id=other.id, day=now.date(), rows_generated=18_500),
                AccessRequest(
                    company_name="Blue Harbor Mobile (synthetic)",
                    contact_name="Alex Doe",
                    email="alex@blueharbor.test",
                    volume_band=VolumeBand.FROM_1K_TO_10K,
                    note="We ship travel eSIMs to retail partners and want branded QR sheets.",
                ),
                AccessRequest(
                    company_name="Sample Roaming Ltd (synthetic)",
                    contact_name="Sam Roe",
                    email="sam@sampleroaming.test",
                    volume_band=VolumeBand.UNDER_1K,
                ),
                AccessRequest(
                    company_name="Old Request Co (synthetic)",
                    contact_name="Jo Poe",
                    email="jo@oldrequest.test",
                    volume_band=VolumeBand.FROM_10K_TO_100K,
                    status=AccessRequestStatus.REJECTED,
                    handled_at=now - timedelta(days=1),
                    handled_by=admin.id,
                ),
            ]
        )
        db.flush()
        secure = User(
            email=MFA_EMAIL,
            organization_id=org.id,
            role=Role.MEMBER,
            password_hash=hash_password(MFA_PASSWORD),
            last_login_at=now - timedelta(days=1),
        )
        new_admin = User(
            email=NEW_ADMIN_EMAIL, is_superadmin=True, role=Role.MEMBER, password_hash=unusable
        )
        db.add_all([secure, new_admin])
        db.flush()
        for with_mfa in (admin, secure):  # 2FA on, with a random secret nobody knows
            with_mfa.mfa_secret = mfa.encrypt_secret(mfa_keys, with_mfa.id, mfa.new_secret())
            with_mfa.mfa_enabled_at = now - timedelta(days=2)
        ops = db.scalars(select(User).where(User.email == "ops@demo-mobile.test")).one()
        # History: every status that is listed, in both organizations (metadata only).
        history = [
            (org.id, owner.id, "orders_november.xlsx", 1_200, 3, 3, 0, BatchStatus.COMPLETED, 2),
            (org.id, ops.id, "travel_esims_week47.csv", 10_000, 20, 7, 0, BatchStatus.PARTIAL, 5),
            (org.id, owner.id, "retail_partners.csv", 2_400, 5, 2, 3, BatchStatus.INCOMPLETE, 50),
            (org.id, ops.id, "reorder_march.csv", 40, 1, 0, 0, BatchStatus.FAILED, 75),
            (org.id, owner.id, "pilot_batch.csv", 25, 1, 1, 1, BatchStatus.COMPLETED, 200),
            (org.id, None, "imported_october.csv", 300, 1, 1, 0, BatchStatus.COMPLETED, 400),
            (other.id, None, "northwind_q4.xlsx", 4_000, 8, 8, 0, BatchStatus.COMPLETED, 30),
            (org.id, owner.id, "sent_by_mistake.csv", 600, 2, 2, 0, BatchStatus.VOIDED, 120),
        ]
        for org_ref, user_ref, name, rows, parts, done, warnings, status, hours in history:
            voided = status == BatchStatus.VOIDED
            db.add(
                Batch(
                    organization_id=org_ref,
                    user_id=user_ref,
                    created_at=now - timedelta(hours=hours),
                    file_name=name,
                    row_count=rows,
                    parts=parts,
                    parts_downloaded=done,
                    warning_count=warnings,
                    error_count=2 if status == BatchStatus.FAILED else 0,
                    status=status,
                    voided_at=now - timedelta(hours=hours - 1) if voided else None,
                    voided_by=owner.id if voided else None,
                )
            )
        db.flush()
        # An unfinished batch with real fingerprints for part 1, so that uploading
        # continue_csv() shows "Continue this batch" (upload-continue).
        _, report = check_upload(continue_csv(), "x.csv")
        unfinished = batches.create_batch(
            db,
            organization_id=org.id,
            user_id=owner.id,
            file_name="orders_week46.csv",
            row_count=CONTINUE_ROWS,
            parts=part_count(CONTINUE_ROWS),
            warning_count=0,
            now=now - timedelta(hours=3),
        )
        batches.record_part(db, org.id, unfinished, 1, part_records(report, 1), key=key, now=now)
        db.flush()
        org_id, admin_id, owner_id, new_admin_id = org.id, admin.id, owner.id, new_admin.id
    with sessions.begin() as db:
        admin_token = accounts.create_session(db, admin_id, now=now)
    with sessions.begin() as db:
        owner_token = accounts.create_session(db, owner_id, now=now)
    with sessions.begin() as db:
        new_admin_token = accounts.create_session(db, new_admin_id, now=now)
    return Seeded(org_id, admin_token, owner_token, new_admin_token)


# --- Server --------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(settings: Settings, sessions: sessionmaker[Session], port: int) -> uvicorn.Server:
    app = create_app(settings, sessions=sessions, run_retention=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            sys.exit("the app did not start")
        time.sleep(0.05)
    return server


# --- Shots ---------------------------------------------------------------------------------


@dataclass
class Pass:
    base: str
    viewport: str
    dark: bool
    seeded: Seeded
    only: set[str] | None
    generate: bool

    def wants(self, name: str) -> bool:
        return self.only is None or any(name.startswith(prefix) for prefix in self.only)

    def shot(self, page: Page, name: str) -> None:
        if not self.wants(name):
            return
        suffix = f"-{self.viewport}" + ("-dark" if self.dark else "")
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready.then(() => true)")
        page.evaluate("window.scrollTo(0, 0)")  # else the sticky top bar lands mid-page
        target = OUTPUT / f"{name}{suffix}.png"
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        print(f"  {target.relative_to(ROOT)}")  # noqa: T201


def upload(page: Page, name: str, data: bytes | None = None) -> None:
    """Choose a data file (synthetic) and wait for the check result."""
    payload = data if data is not None else (FIXTURES / name).read_bytes()
    page.set_input_files("#file", files=[{"name": name, "mimeType": "text/csv", "buffer": payload}])
    page.wait_for_selector("#results > .results", timeout=60_000)


def anonymous_pages(p: Pass, page: Page) -> None:
    page.goto(f"{p.base}/login")
    p.shot(page, "login")
    if p.wants("login-focus") and p.viewport == "desktop":
        for _ in range(3):  # skip link, brand, email
            page.keyboard.press("Tab")
        p.shot(page, "login-focus")
    page.fill("#email", "someone@demo-mobile.test")
    page.fill("#password", WRONG_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("p.error")
    p.shot(page, "login-error")

    if p.wants("login-2fa"):  # the second step: the code page, then a wrong code
        page.goto(f"{p.base}/login")
        page.fill("#email", MFA_EMAIL)
        page.fill("#password", MFA_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_selector("#code")
        p.shot(page, "login-2fa")
        page.fill("#code", "000000")
        page.click("button[type=submit]")
        page.wait_for_selector("p.error")
        p.shot(page, "login-2fa-error")

    page.goto(f"{p.base}/request-access")
    p.shot(page, "request-access")
    page.fill("#company_name", "Example Travel SIMs (synthetic)")
    page.fill("#contact_name", "Taylor Example")
    page.fill("#email", "taylor@example-travel.test")
    page.select_option("#volume_band", index=2)
    page.fill("#note", "Synthetic request made by scripts/screens.py.")
    page.click("button[type=submit]")
    page.wait_for_selector("text=We have received your request")
    p.shot(page, "request-received")

    page.goto(f"{p.base}/invite/{'x' * 43}")
    p.shot(page, "link-invalid")


def folder_mode_pages(p: Pass, page: Page) -> None:
    """A multi-part order saved into a folder: choice, progress, cancel, resume, done."""
    page.add_init_script(FAKE_FOLDER)
    page.goto(f"{p.base}/")
    page.fill("#company_name", "Demo Mobile")
    upload(page, "big_1200.csv", make_csv(MULTI_PART_ROWS))
    page.click("#download")
    page.wait_for_selector("#choose-folder:not([hidden])")
    p.shot(page, "upload-folder-choice")
    page.click("#choose-folder")
    page.wait_for_selector("#generating:has-text('Saved part 1 of 3')", timeout=180_000)
    p.shot(page, "upload-folder-progress")
    page.click("#cancel")
    page.wait_for_selector("#retry:not([hidden])", timeout=180_000)
    p.shot(page, "upload-folder-cancelled")
    page.click("#retry")
    page.wait_for_selector("#generating:has-text('Done')", timeout=180_000)
    p.shot(page, "upload-folder-done")


def owner_pages(p: Pass, page: Page) -> None:
    page.add_init_script(NO_FOLDER_API)  # the folder-mode shots have their own page
    page.goto(f"{p.base}/")
    p.shot(page, "upload-empty")
    if p.wants("upload-focus") and p.viewport == "desktop":
        page.focus("#file")
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")  # keyboard focus, so :focus-visible applies
        p.shot(page, "upload-focus")

    upload(page, "mixed_valid_invalid.csv")
    p.shot(page, "upload-errors")
    upload(page, "warning_iccid_prefix.csv")
    p.shot(page, "upload-warnings")
    upload(page, "valid_full.csv")
    p.shot(page, "upload-valid")
    upload(page, "orders_week46.csv", continue_csv())  # the seeded unfinished batch's file
    p.shot(page, "upload-continue")

    shots = (
        "upload-done",
        "upload-multipart",
        "upload-parts",
        "upload-parts-downloaded",
        "upload-progress",
        "upload-cancelled",
    )
    if p.generate and any(p.wants(name) for name in shots):
        page.fill("#company_name", "Demo Mobile")
        page.click("#download")
        page.wait_for_selector("#generating:has-text('Done')", timeout=120_000)
        p.shot(page, "upload-done")

        # Several parts without the folder API: the parts list, one part at a time.
        upload(page, "big_1200.csv", make_csv(MULTI_PART_ROWS))
        p.shot(page, "upload-multipart")
        page.click("#download")
        page.wait_for_selector("#parts-manual:not([hidden])")
        p.shot(page, "upload-parts")
        page.click("#next-part")
        page.wait_for_selector("#generating:has-text('Generating part 1 of 3')")
        p.shot(page, "upload-progress")
        page.wait_for_selector("#part-list tr[data-part='1'] .badge-success", timeout=180_000)
        page.wait_for_selector("#next-part:not([disabled])")
        p.shot(page, "upload-parts-downloaded")
        page.click("#next-part")
        page.wait_for_selector("#generating:has-text('Generating part 2 of 3')")
        page.click("#cancel")
        page.wait_for_selector("#generating:has-text('Cancelled')")
        p.shot(page, "upload-cancelled")

    if p.generate and p.wants("upload-folder"):
        folder_page = page.context.new_page()
        try:
            folder_mode_pages(p, folder_page)
        finally:
            folder_page.close()

    page.goto(f"{p.base}/team")
    p.shot(page, "team")
    history_pages(p, page)
    if p.wants("account"):
        account_pages(p, page)


def account_pages(p: Pass, page: Page) -> None:
    """The owner sets up 2FA: QR code and key, the recovery codes, then the status."""
    page.goto(f"{p.base}/account/2fa")
    page.wait_for_selector("svg.qr-code")
    p.shot(page, "account-2fa-setup")
    key = page.inner_text("#text-key").replace(" ", "")
    secret = base64.b32decode(key + "=" * (-len(key) % 8))
    page.fill("#enable-code", mfa.code_at(secret, mfa.step_of(datetime.now(UTC))))
    page.click("form[action='/account/2fa/enable'] button[type=submit]")
    page.wait_for_selector("#recovery-codes")
    p.shot(page, "account-2fa-recovery-codes")
    page.goto(f"{p.base}/account/2fa")
    p.shot(page, "account-2fa-on")


def history_pages(p: Pass, page: Page) -> None:
    """History: the list, the calendar opened by keyboard, and a date error."""
    page.goto(f"{p.base}/history")
    p.shot(page, "history")
    if p.wants("history-calendar"):
        page.focus("#from_date")
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        page.wait_for_selector(".datepicker-popup")
        page.keyboard.press("ArrowRight")
        p.shot(page, "history-calendar")
        page.keyboard.press("Escape")
    page.click("a:has-text('Void batch')")  # the newest batch that can be voided
    page.wait_for_selector("#confirm_not_sent")
    p.shot(page, "history-void-confirm")
    page.goto(f"{p.base}/history?from_date=31/02/2026&to_date=")
    p.shot(page, "history-error")


def admin_pages(p: Pass, page: Page, anon: Page) -> None:
    page.goto(f"{p.base}/admin/orgs")
    p.shot(page, "admin-orgs")
    page.goto(f"{p.base}/admin/orgs/{p.seeded.org_id}")
    p.shot(page, "admin-org")
    page.goto(f"{p.base}/admin/requests")
    p.shot(page, "admin-requests")
    page.goto(f"{p.base}/admin/requests?show=handled")
    p.shot(page, "admin-requests-handled")
    page.goto(f"{p.base}/history")
    p.shot(page, "admin-history")
    page.goto(f"{p.base}/")
    p.shot(page, "admin-upload-no-organization")

    # An invite through the real form: the one-time link page, then the link itself.
    page.goto(f"{p.base}/admin/orgs/{p.seeded.org_id}")
    page.fill("#invite_email", f"invitee.{p.viewport}{'.dark' if p.dark else ''}@demo-mobile.test")
    page.click("form[action$='/invite'] button[type=submit]")
    page.wait_for_selector("#one-time-link")
    p.shot(page, "link-created")
    link = page.input_value("#one-time-link")
    anon.goto(link)
    p.shot(anon, "set-password")


def run_pass(browser_new_context: Callable[..., BrowserContext], p: Pass) -> None:
    width, height = VIEWPORTS[p.viewport]
    options = {
        "viewport": {"width": width, "height": height},
        "device_scale_factor": 2 if p.viewport == "phone" else 1,
        "is_mobile": p.viewport == "phone",
        "has_touch": p.viewport == "phone",
        "color_scheme": "dark" if p.dark else "light",
        "accept_downloads": False,  # nothing is saved to disk
    }
    who_all = ("anon", "owner", "admin", "new_admin")
    contexts = {who: browser_new_context(**options) for who in who_all}
    try:
        for who, token in (
            ("owner", p.seeded.owner_token),
            ("admin", p.seeded.admin_token),
            ("new_admin", p.seeded.new_admin_token),
        ):
            contexts[who].add_cookies([{"name": SESSION_COOKIE, "value": token, "url": p.base}])
        anon = contexts["anon"].new_page()
        anonymous_pages(p, anon)
        owner_pages(p, contexts["owner"].new_page())
        admin_pages(p, contexts["admin"].new_page(), contexts["anon"].new_page())
        if p.wants("admin-2fa-required"):  # a superadmin without 2FA, sent to set it up
            forced = contexts["new_admin"].new_page()
            forced.goto(f"{p.base}/admin/orgs")
            forced.wait_for_selector(".mfa-required")
            p.shot(forced, "admin-2fa-required")
    finally:
        for context in contexts.values():
            context.close()


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--only", default="", help="comma-separated page-name prefixes")
    args.add_argument("--skip-generate", action="store_true", help="no PDF generation shots")
    args.add_argument("--no-dark", action="store_true", help="light scheme only")
    opts = args.parse_args()
    only = {s.strip() for s in opts.only.split(",") if s.strip()} or None

    engine = test_engine()
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    fingerprint_key = secrets.token_urlsafe(48).encode()
    mfa_key = secrets.token_urlsafe(48)
    settings = Settings(
        env="development",
        database_url=Secret(engine.url.render_as_string(hide_password=False)),
        secret_key=Secret(secrets.token_urlsafe(48)),
        fingerprint_key=Secret(fingerprint_key.decode()),
        mfa_key=Secret(mfa_key),
        public_base_url=base,
    )
    mfa_keys = mfa.keys_from(mfa_key)
    server = start_server(settings, sessions, port)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    passes = [("desktop", False), ("phone", False)]
    if not opts.no_dark:
        passes += [("desktop", True), ("phone", True)]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for viewport, dark in passes:
                    print(f"{viewport}{' dark' if dark else ''}:")  # noqa: T201
                    run_pass(
                        browser.new_context,
                        Pass(
                            base=base,
                            viewport=viewport,
                            dark=dark,
                            seeded=seed(engine, sessions, fingerprint_key, mfa_keys),
                            only=only,
                            generate=not opts.skip_generate and not dark,
                        ),
                    )
            finally:
                browser.close()
    finally:
        server.should_exit = True
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

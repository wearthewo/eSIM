"""The whole product in a real browser, from the first superadmin to a disabled organization.

Run with ``pytest -m e2e`` (needs TEST_DATABASE_URL and ``playwright install chromium``).
Separate browser contexts per person; a screenshot after each step in ``output/e2e/``.
See tests/e2e/conftest.py for the server, the watchers and the helpers. All synthetic.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from sqlalchemy.orm import Session

from app import admin
from app.db.models import DEFAULT_DAILY_ROW_QUOTA
from tests.conftest import FIXTURES_DIR, synthetic_iccid
from tests.e2e.conftest import (
    NOTIFY_EMAIL,
    OPERATOR_NAME,
    SUPERADMIN_EMAIL,
    Authenticator,
    Journey,
    Person,
    Server,
    link_in,
)

pytestmark = pytest.mark.e2e

COMPANY = "E2E Travel Mobile (synthetic)"
OWNER_EMAIL = "owner@e2e-travel.test"
MEMBER_EMAIL = "member@e2e-travel.test"
SUPERADMIN_PASSWORD = "e2e superadmin passphrase"  # synthetic
OWNER_PASSWORD = "e2e owner passphrase one"  # synthetic
OWNER_PASSWORD_2 = "e2e owner passphrase two"  # synthetic
MEMBER_PASSWORD = "e2e member passphrase one"  # synthetic
MEMBER_PASSWORD_2 = "e2e member passphrase two"  # synthetic
CONSENT = "By continuing you agree to the Terms and Privacy Policy."
ATHENS_TIME = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")  # dd/mm/yyyy HH:mm
SMALL_FILE = "valid_full.csv"
BIG_FILE = "e2e_orders_1200.csv"
BIG_ROWS = 1_200
GENERATE_TIMEOUT_MS = 180_000


def big_csv(rows: int = BIG_ROWS, start: int = 100_001) -> bytes:
    """Synthetic rows with their own ICCIDs and codes (never those of valid_full.csv)."""
    lines = ["iccid,activation_code,customer_name,email"]
    for n in range(start, start + rows):
        lines.append(
            f"{synthetic_iccid(n)},LPA:1$smdp.example.invalid$E2E-{n:06d}-AAAA,"
            f"Test Customer {n},customer{n}@mail.example.invalid"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


# --- Page helpers ------------------------------------------------------------------------------


def assert_consent(p: Person) -> None:
    note = p.page.locator("p.form-note")
    assert note.is_visible(), p.path
    assert note.inner_text().strip() == CONSENT


def set_password(p: Person, password: str) -> None:
    p.page.fill("#password", password)
    p.page.fill("#password_confirm", password)
    p.page.click("form.auth-form button[type=submit]")


def log_in(p: Person, email: str, password: str) -> None:
    p.goto("/login")
    p.page.fill("#email", email)
    p.page.fill("#password", password)
    p.page.click("form.auth-form button[type=submit]")
    p.page.wait_for_load_state("load")


def log_out(p: Person) -> None:
    p.page.click("form.logout button[type=submit]")
    p.page.wait_for_url(f"{p.base}/login")


def enter_code(p: Person, code: str) -> None:
    assert p.path == "/login/2fa", p.path
    p.page.fill("#code", code)
    p.page.click("form.auth-form button[type=submit]")
    p.page.wait_for_load_state("load")


def upload(p: Person, name: str, data: bytes) -> str:
    """Choose a data file (in memory) and wait for the new check result; its status text."""
    p.page.evaluate(
        "document.querySelector('#results > .results')?.setAttribute('data-e2e-old', '')"
    )
    p.page.set_input_files("#file", files=[{"name": name, "mimeType": "text/csv", "buffer": data}])
    p.page.wait_for_selector("#results > .results:not([data-e2e-old])", timeout=60_000)
    status = p.page.locator("#results .results-status, #results p.error").first
    return status.inner_text().strip()


def generate(p: Person, click: Callable[[], None]) -> list[str]:
    """Click, and return the member names of the ZIP the browser receives (in memory)."""

    def is_zip(response: object) -> bool:
        return urlsplit(response.url).path == "/generate" and response.status == 200

    with p.page.expect_response(is_zip, timeout=GENERATE_TIMEOUT_MS) as info:
        click()
    body = info.value.body()
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return zf.namelist()


def assert_zip(names: list[str], pdfs: int, *, mailmerge: bool) -> None:
    expected_extra = {"manifest.xlsx"} | ({"mailmerge.xlsx"} if mailmerge else set())
    pdf_names = [n for n in names if n.startswith("esim_") and n.endswith(".pdf")]
    assert len(pdf_names) == pdfs, (len(pdf_names), pdfs)
    assert set(names) - set(pdf_names) == expected_extra, names


def history_row(p: Person, file_name: str) -> dict[str, str]:
    """The History row of ``file_name`` (the newest if several): its cells by column."""
    p.goto("/history")
    row = p.page.locator("tr[data-batch]", has=p.page.locator("td.file-name", has_text=file_name))
    row = row.first
    cells = row.locator("td").all_inner_texts()
    return {
        "id": row.get_attribute("data-batch") or "",
        "when": cells[0].strip(),
        "parts": cells[4].strip(),
        "status": cells[6].strip(),
        "actions": cells[7].strip() if len(cells) > 7 else "",
    }


@contextmanager
def session_scope(server: Server) -> Iterator[Session]:
    with server.sessions.begin() as db:
        yield db


# --- The journey -------------------------------------------------------------------------------


def test_journey(journey: Journey, server: Server) -> None:
    small = (FIXTURES_DIR / SMALL_FILE).read_bytes()
    big = big_csv()

    # 1. The deploy's bootstrap creates the superadmin, who must set up 2FA first.
    with journey.step("01 superadmin: bootstrap, password, forced 2FA"):
        out, err = io.StringIO(), io.StringIO()
        code = admin.main(
            ["bootstrap"],
            session_scope=lambda: session_scope(server),
            settings=server.settings,
            out=out,
            err=err,
        )
        assert code == 0, err.getvalue()
        line = out.getvalue().strip()
        assert line.startswith(admin.BOOTSTRAP_LABEL), line
        link = line.split()[-1]

        sa = journey.person("superadmin")
        sa.goto(link)
        assert_consent(sa)
        journey.shot(sa, "set-password")
        set_password(sa, SUPERADMIN_PASSWORD)
        sa.page.wait_for_url(f"{sa.base}/account/2fa")
        assert sa.page.locator(".mfa-required").is_visible()
        journey.shot(sa, "2fa-required")
        sa_auth = Authenticator.from_key(sa.page.inner_text("#text-key"))
        sa.page.fill("#enable-code", sa_auth.code())
        sa.page.click("form[action='/account/2fa/enable'] button[type=submit]")
        sa.page.wait_for_selector("#recovery-codes")
        recovery_codes = [
            c.strip() for c in sa.page.locator("#recovery-codes code").all_inner_texts()
        ]
        assert len(recovery_codes) == 10
        journey.shot(sa, "recovery-codes")
        sa.page.click("a:has-text('Continue')")
        sa.page.click("nav.site-nav a:has-text('Admin')")
        sa.page.wait_for_url(f"{sa.base}/admin/orgs")
        journey.shot(sa, "admin")

    # 2. A visitor reads the legal pages, requests access, is approved and becomes the owner.
    with journey.step("02 visitor: legal pages, request access, approval, owner invite"):
        owner = journey.person("owner")
        owner.goto("/login")
        journey.shot(owner, "login")
        for label, path in (("Privacy Policy", "/privacy"), ("Terms of Use", "/terms")):
            owner.page.click(f"footer.site-footer a:has-text('{label}')")
            owner.page.wait_for_url(f"{owner.base}{path}")
            assert OPERATOR_NAME in owner.page.content()
            mailto = owner.page.locator(f"a[href='mailto:{server.settings.contact_email}']")
            assert mailto.count() >= 1
            journey.shot(owner, path.strip("/"))
        owner.goto("/login")
        owner.page.click("a:has-text('Request access')")
        owner.page.wait_for_url(f"{owner.base}/request-access")
        assert_consent(owner)
        owner.page.fill("#company_name", COMPANY)
        owner.page.fill("#contact_name", "Erin Example")
        owner.page.fill("#email", OWNER_EMAIL)
        owner.page.select_option("#volume_band", index=2)
        owner.page.fill("#note", "Synthetic request from the e2e journey.")
        journey.shot(owner, "request-access")
        owner.page.click("form.auth-form button[type=submit]")
        owner.page.wait_for_selector("text=We have received your request")
        journey.shot(owner, "request-received")

        notice = server.mail(NOTIFY_EMAIL, "New access request")
        assert COMPANY in notice.text and OWNER_EMAIL not in notice.text
        sa.goto(link_in(notice, "/admin/requests"))
        row = sa.page.locator("tr", has_text=COMPANY)
        row.locator("button:has-text('Approve')").click()
        sa.page.wait_for_selector("text=Invitation sent to")
        journey.shot(sa, "approved")

        invite = server.mail(OWNER_EMAIL, "Your invitation")
        assert COMPANY in invite.text and "as an owner" in invite.text
        owner.goto(link_in(invite))
        assert_consent(owner)
        journey.shot(owner, "invite")
        set_password(owner, OWNER_PASSWORD)
        owner.page.wait_for_url(f"{owner.base}/")
        assert owner.page.locator("#file").count() == 1  # the upload page, no 2FA prompt
        journey.shot(owner, "upload-page")

    # 3. Uploads: one ZIP, then a 1,200-row order downloaded part by part (2 of 3 parts).
    with journey.step("03 owner: single ZIP, then parts 1-2 of 3"):
        owner.page.fill("#company_name", "E2E Travel Mobile")
        assert upload(owner, SMALL_FILE, small) == "All rows passed the check"
        names = generate(owner, lambda: owner.page.click("#download"))
        assert_zip(names, 5, mailmerge=False)
        owner.page.wait_for_selector("#generating:has-text('Done')")
        journey.shot(owner, "single-zip-done")

        assert upload(owner, BIG_FILE, big) == "All rows passed the check"
        assert owner.page.inner_text("#download").strip() == "Download 3 ZIP files"
        journey.shot(owner, "big-checked")
        owner.page.click("#download")
        owner.page.wait_for_selector("#parts-manual:not([hidden])")
        journey.shot(owner, "parts-list")
        for part, rows in ((1, 500), (2, 500)):
            names = generate(owner, lambda: owner.page.click("#next-part"))
            assert_zip(names, rows, mailmerge=True)
            owner.page.wait_for_selector(
                f"#part-list tr[data-part='{part}'] .badge-success", timeout=GENERATE_TIMEOUT_MS
            )
            owner.page.wait_for_selector("#next-part:not([disabled])")
        journey.shot(owner, "parts-1-2-downloaded")

        small_row = history_row(owner, SMALL_FILE)
        big_row = history_row(owner, BIG_FILE)
        assert ATHENS_TIME.match(small_row["when"]), small_row
        assert ATHENS_TIME.match(big_row["when"]), big_row
        assert (small_row["parts"], small_row["status"]) == ("1 / 1", "Completed")
        assert (big_row["parts"], big_row["status"]) == ("2 / 3", "Partial")
        journey.shot(owner, "history-partial")

    # 4. The same file again continues the unfinished batch.
    with journey.step("04 owner: continue the batch, part 3"):
        owner.goto("/")
        owner.page.fill("#company_name", "E2E Travel Mobile")
        upload(owner, BIG_FILE, big)
        assert owner.page.locator("#continue-batch").is_visible()
        assert owner.page.inner_text("#download").strip() == "Continue this batch"
        journey.shot(owner, "continue")
        owner.page.click("#download")
        owner.page.wait_for_selector("#parts-manual:not([hidden])")
        for part in (1, 2):
            assert owner.page.locator(f"#part-list tr[data-part='{part}'] .badge-success").count()
        names = generate(owner, lambda: owner.page.click("#next-part"))
        assert_zip(names, BIG_ROWS - 1_000, mailmerge=True)
        owner.page.wait_for_selector(
            "#generating:has-text('Done: all 3 parts were downloaded.')",
            timeout=GENERATE_TIMEOUT_MS,
        )
        journey.shot(owner, "continue-done")
        big_row = history_row(owner, BIG_FILE)
        assert (big_row["parts"], big_row["status"]) == ("3 / 3", "Completed")
        journey.shot(owner, "history-completed")

    # 5. Voiding a batch lets its codes be used again.
    with journey.step("05 owner: void, then the file passes again"):
        owner.goto("/")
        with owner.expect_error(r"status of 4\d\d.*/validate"):
            status = upload(owner, SMALL_FILE, small)
        assert status == "Some rows have errors", status
        assert owner.page.locator("#used-before").is_visible()
        journey.shot(owner, "already-used")
        small_row = history_row(owner, SMALL_FILE)
        owner.page.click(f"tr[data-batch='{small_row['id']}'] a:has-text('Void batch')")
        owner.page.check("#confirm_not_sent")
        journey.shot(owner, "void-confirm")
        owner.page.click("form.void-form button[type=submit]")
        owner.page.wait_for_load_state("load")
        assert history_row(owner, SMALL_FILE)["status"].startswith("Voided")
        journey.shot(owner, "voided")
        owner.goto("/")
        assert upload(owner, SMALL_FILE, small) == "All rows passed the check"
        journey.shot(owner, "passes-after-void")

    # 6. A member, invited by email from Team, cannot void and has no Team page.
    with journey.step("06 owner invites a member; member's limits"):
        owner.goto("/team")
        owner.page.fill("#invite_email", MEMBER_EMAIL)
        owner.page.click("form[action='/team/invite'] button[type=submit]")
        owner.page.wait_for_selector("text=Invitation sent to")
        journey.shot(owner, "member-invited")
        member = journey.person("member")
        member.goto(link_in(server.mail(MEMBER_EMAIL, "Your invitation")))
        assert_consent(member)
        set_password(member, MEMBER_PASSWORD)
        member.page.wait_for_url(f"{member.base}/")
        member.goto("/history")
        assert member.page.locator("tr[data-batch]").count() >= 2
        assert member.page.locator("a:has-text('Void batch')").count() == 0
        journey.shot(member, "history-no-void")
        with member.expect_error(r"status of 404"):
            assert member.page.goto(f"{member.base}/team").status == 404
            void_url = f"{member.base}/history/batches/{big_row['id']}/void"
            assert member.page.goto(void_url).status == 404
        journey.shot(member, "team-404")

    # 7. The daily quota.
    with journey.step("07 superadmin: quota 10 refuses, then restored"):

        def set_quota(value: int) -> None:
            sa.goto("/admin/orgs")
            sa.page.click(f"a:has-text('{COMPANY}')")
            sa.page.fill("#daily_row_quota", str(value))
            sa.page.click("form[action$='/quota'] button[type=submit]")
            sa.page.wait_for_load_state("load")
            assert sa.page.input_value("#daily_row_quota") == str(value)

        set_quota(10)
        journey.shot(sa, "quota-10")
        owner.goto("/")
        with owner.expect_error(r"status of 4\d\d.*/validate"):
            message = upload(owner, SMALL_FILE, small)
        assert message.startswith("Daily limit reached:") and "of 10 rows used today" in message
        journey.shot(owner, "quota-refused")
        set_quota(DEFAULT_DAILY_ROW_QUOTA)
        owner.goto("/")
        assert upload(owner, SMALL_FILE, small) == "All rows passed the check"
        journey.shot(owner, "quota-restored")

    # 8. The member resets their password; the link works once.
    with journey.step("08 member: forgot password, reset, link used twice"):
        member.goto("/")  # still on the 404 of step 6, which has no Log out button
        log_out(member)
        member.page.click("a:has-text('Forgot password?')")
        member.page.wait_for_url(f"{member.base}/forgot-password")
        assert_consent(member)
        member.page.fill("#email", MEMBER_EMAIL)
        member.page.click("form.auth-form button[type=submit]")
        member.page.wait_for_selector("text=Check your email")
        journey.shot(member, "forgot-sent")
        reset_link = link_in(server.mail(MEMBER_EMAIL, "Reset your"))
        member.goto(reset_link)
        assert_consent(member)
        set_password(member, MEMBER_PASSWORD_2)
        member.page.wait_for_url(f"{member.base}/")
        log_out(member)
        log_in(member, MEMBER_EMAIL, MEMBER_PASSWORD_2)
        assert member.path == "/", member.path
        journey.shot(member, "signed-in-new-password")
        with member.expect_error(r"status of 404"):
            assert member.page.goto(reset_link).status == 404
        assert member.page.locator("h1:has-text('Link not valid')").is_visible()
        journey.shot(member, "reset-link-used")
        member.goto("/")
        assert member.path == "/"  # the failed link did not end the session

    # 9. The owner turns 2FA on; a password reset does not switch it off.
    with journey.step("09 owner: optional 2FA on, code at login, still after a reset"):
        owner.goto("/account/2fa")
        owner_auth = Authenticator.from_key(owner.page.inner_text("#text-key"))
        owner.page.fill("#enable-code", owner_auth.code())
        owner.page.click("form[action='/account/2fa/enable'] button[type=submit]")
        owner.page.wait_for_selector("#recovery-codes")
        journey.shot(owner, "2fa-on")
        log_out(owner)
        log_in(owner, OWNER_EMAIL, OWNER_PASSWORD)
        journey.shot(owner, "login-2fa")
        enter_code(owner, owner_auth.code())
        assert owner.path == "/", owner.path
        log_out(owner)
        owner.page.click("a:has-text('Forgot password?')")
        owner.page.fill("#email", OWNER_EMAIL)
        owner.page.click("form.auth-form button[type=submit]")
        owner.page.wait_for_selector("text=Check your email")
        owner.goto(link_in(server.mail(OWNER_EMAIL, "Reset your")))
        set_password(owner, OWNER_PASSWORD_2)
        owner.page.wait_for_url(f"{owner.base}/login/2fa")
        journey.shot(owner, "reset-still-needs-code")
        enter_code(owner, owner_auth.code())
        assert owner.path == "/", owner.path
        journey.shot(owner, "signed-in-after-reset")

    # 10. A disabled organization: its people are sent to login and cannot sign in.
    with journey.step("10 superadmin disables the org; owner and member locked out"):
        sa.goto("/admin/orgs")
        sa.page.click(f"a:has-text('{COMPANY}')")
        sa.page.click("button:has-text('Disable organization')")
        sa.page.wait_for_selector("button:has-text('Enable organization')")
        journey.shot(sa, "org-disabled")
        for p, email, password in (
            (owner, OWNER_EMAIL, OWNER_PASSWORD_2),
            (member, MEMBER_EMAIL, MEMBER_PASSWORD_2),
        ):
            p.goto("/")
            assert p.path == "/login", (p.name, p.path)
            with p.expect_error(r"status of 401"):
                log_in(p, email, password)
            assert p.path == "/login"
            assert p.page.locator("p.error:has-text('Email or password is incorrect')").count()
            journey.shot(p, "locked-out")

    # 11. The superadmin signs in with a recovery code, which then no longer works.
    with journey.step("11 superadmin: recovery code works once"):
        log_out(sa)
        log_in(sa, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD)
        enter_code(sa, recovery_codes[0])
        assert sa.path == "/", sa.path
        journey.shot(sa, "recovery-code-used")
        log_out(sa)
        log_in(sa, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD)
        with sa.expect_error(r"status of 401"):
            enter_code(sa, recovery_codes[0])
        assert sa.path == "/login/2fa"
        assert sa.page.locator("p.error:has-text('The code is incorrect')").count()
        journey.shot(sa, "recovery-code-reused")

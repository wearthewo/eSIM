"""End-to-end fixtures: a real uvicorn server on the TEST database, Chromium, people.

- The server runs in a thread of this process with ``FakeMailer``, so the test reads the
  emails the app sends (and "Forgot password?" is on). Settings are validated like
  production's (random keys, operator, contact, notify address) except ``ENV``: plain
  http, cookies without ``__Host-``.
- Each person has their own browser context. Downloads are refused (``accept_downloads``
  off), so no ZIP or PDF is ever written: ZIPs are read in memory from the /generate
  response. The parts list is used instead of the folder picker.
- Console errors, page errors and CSP violations fail the step they happen in, unless
  the step declared them (``person.expect_error``): a 401 or 404 page also logs "Failed
  to load resource" in Chromium.
- Screenshots go to ``output/e2e/`` (gitignored), step timings to the terminal summary
  and ``output/e2e/timings.txt``.

All data is synthetic (``*.test`` addresses, ``8999`` ICCIDs, ``smdp.example.invalid``).
"""

from __future__ import annotations

import base64
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import mfa
from app.db.base import Base
from app.mail import FakeMailer, Message
from app.settings import Settings, load_settings
from app.web.main import create_app
from tests.conftest import ROOT

playwright_api = pytest.importorskip("playwright.sync_api")
Browser = playwright_api.Browser
BrowserContext = playwright_api.BrowserContext
ConsoleMessage = playwright_api.ConsoleMessage
Page = playwright_api.Page

OUTPUT = ROOT / "output" / "e2e"
TIMINGS: list[tuple[str, float]] = []

SUPERADMIN_EMAIL = "superadmin@platform.test"
NOTIFY_EMAIL = "requests@platform.test"
OPERATOR_NAME = "Synthetic Operator Ltd (e2e)"
CONTACT_EMAIL = "support@platform.test"

# The parts list (one part per button), as in a browser without the File System Access API.
NO_FOLDER_API = """
delete window.showDirectoryPicker;
if (typeof window.showDirectoryPicker === "function") window.showDirectoryPicker = undefined;
"""
# Every CSP violation is reported to the test through a binding (survives navigations).
CSP_WATCH = """
document.addEventListener("securitypolicyviolation", function (e) {
  window.__esimCspViolation(e.violatedDirective + " blocked " + (e.blockedURI || "inline") +
    " at " + (e.sourceFile || document.location.pathname) + ":" + e.lineNumber);
});
"""
# A /generate cache miss is answered 428 and re-sent with the file by design (app.js).
ALWAYS_ALLOWED = re.compile(r"status of 428")
LINK = re.compile(r"https?://127\.0\.0\.1:\d+/\S+")


# --- Server ------------------------------------------------------------------------------------


@dataclass
class Server:
    base: str
    settings: Settings
    mailer: FakeMailer
    sessions: sessionmaker[Session]
    _seen: set[int] = field(default_factory=set)

    def mail(self, to: str, subject: str, *, timeout: float = 10) -> Message:
        """The next email to ``to`` whose subject contains ``subject`` (not returned before).

        Some emails are sent in a background task after the response, so this waits."""
        deadline = time.monotonic() + timeout
        while True:
            for i, message in enumerate(list(self.mailer.outbox)):
                if i not in self._seen and message.to == to and subject in message.subject:
                    self._seen.add(i)
                    return message
            if time.monotonic() > deadline:
                raise AssertionError(f"no email {subject!r} to {to} within {timeout} s")
            time.sleep(0.1)


def link_in(message: Message, path: str = "/invite/") -> str:
    """The first link in the email's text part whose path starts with ``path``."""
    for url in LINK.findall(message.text):
        if path in url:
            return url
    raise AssertionError(f"no {path} link in the email {message.subject!r}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _truncate(engine: Engine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="module")
def server(db_engine: Engine) -> Iterator[Server]:
    _truncate(db_engine)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = load_settings(
        {
            "DATABASE_URL": db_engine.url.render_as_string(hide_password=False),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "FINGERPRINT_KEY": secrets.token_urlsafe(48),
            "MFA_KEY": secrets.token_urlsafe(48),
            "PUBLIC_BASE_URL": base,
            "BOOTSTRAP_SUPERADMIN_EMAIL": SUPERADMIN_EMAIL,
            "ADMIN_NOTIFY_EMAIL": NOTIFY_EMAIL,
            "OPERATOR_NAME": OPERATOR_NAME,
            "CONTACT_EMAIL": CONTACT_EMAIL,
            "EMAIL_FROM": "eSIM Sheet <no-reply@mail.platform.test>",
        }
    )
    sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
    mailer = FakeMailer()
    app = create_app(settings, sessions=sessions, run_retention=False, mailer=mailer)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not uv.started:
        assert thread.is_alive(), "uvicorn stopped"
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        yield Server(base, settings, mailer, sessions)
    finally:
        uv.should_exit = True
        thread.join(timeout=15)


# --- Browser and people -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with playwright_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except playwright_api.Error as exc:
            pytest.skip(f"Chromium is not installed (playwright install chromium): {exc}")
        try:
            yield chromium
        finally:
            chromium.close()


class Person:
    """One person: a browser context and its page, watched for console and CSP errors."""

    def __init__(self, name: str, context: BrowserContext, base: str) -> None:
        self.name = name
        self.context = context
        self.base = base
        self.errors: list[str] = []
        self.allowed: list[re.Pattern[str]] = []
        context.expose_binding(
            "__esimCspViolation", lambda _source, detail: self.errors.append(f"CSP: {detail}")
        )
        context.add_init_script(CSP_WATCH + NO_FOLDER_API)
        context.on("console", self._console)
        context.on("weberror", lambda error: self.errors.append(f"page error: {error.error}"))
        self.page: Page = context.new_page()
        self.page.set_default_timeout(30_000)

    def _console(self, message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        detail = f"{message.text} ({message.location.get('url', '')})"
        if ALWAYS_ALLOWED.search(message.text) or any(p.search(detail) for p in self.allowed):
            return
        self.errors.append(f"console: {detail}")

    def flush(self) -> None:
        """Let Playwright deliver pending events (they arrive before this call's answer)."""
        if not self.page.is_closed():
            self.page.evaluate("0")

    @contextmanager
    def expect_error(self, pattern: str) -> Iterator[None]:
        """Console errors matching ``pattern`` are expected inside the block."""
        compiled = re.compile(pattern)
        self.allowed.append(compiled)
        try:
            yield
            self.flush()
        finally:
            self.allowed.remove(compiled)

    def goto(self, path: str) -> None:
        self.page.goto(self.base + path if path.startswith("/") else path)

    @property
    def path(self) -> str:
        return self.page.url.removeprefix(self.base).split("?", 1)[0]

    def check(self) -> None:
        self.flush()
        problems, self.errors = self.errors, []
        assert not problems, f"{self.name}: " + "; ".join(problems)


@dataclass
class Journey:
    server: Server
    browser: Browser
    people: list[Person] = field(default_factory=list)
    shots: list[Path] = field(default_factory=list)
    _step: str = ""

    def person(self, name: str) -> Person:
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, accept_downloads=False
        )
        person = Person(name, context, self.server.base)
        self.people.append(person)
        return person

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self._step = name
        started = time.perf_counter()
        try:
            yield
            for person in self.people:
                person.check()
        finally:
            TIMINGS.append((name, time.perf_counter() - started))

    def shot(self, person: Person, name: str) -> None:
        page = person.page
        page.wait_for_load_state("load")
        target = OUTPUT / f"{len(self.shots) + 1:02d}-{person.name}-{name}.png"
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        self.shots.append(target)


@pytest.fixture
def journey(server: Server, browser: Browser) -> Iterator[Journey]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT.glob("*.png"):
        old.unlink()
    TIMINGS.clear()
    j = Journey(server, browser)
    try:
        yield j
    finally:
        for person in j.people:
            person.context.close()
        _write_timings()


def _write_timings() -> None:
    if TIMINGS:
        lines = [f"{seconds:7.1f} s  {name}" for name, seconds in TIMINGS]
        lines.append(f"{sum(s for _, s in TIMINGS):7.1f} s  total")
        (OUTPUT / "timings.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not TIMINGS:
        return
    terminalreporter.section("e2e journey timings")
    for name, seconds in TIMINGS:
        terminalreporter.write_line(f"{seconds:7.1f} s  {name}")
    terminalreporter.write_line(f"{sum(s for _, s in TIMINGS):7.1f} s  total")


# --- Two-factor codes --------------------------------------------------------------------------


@dataclass
class Authenticator:
    """An authenticator app: a code for the next 30-second step not used yet (the app
    refuses a step it already accepted, so a second sign-in may wait up to 30 s)."""

    secret: bytes
    last_step: int = 0

    @classmethod
    def from_key(cls, key: str) -> Authenticator:
        key = key.replace(" ", "")
        return cls(base64.b32decode(key + "=" * (-len(key) % 8)))

    def code(self, *, sleep: Callable[[float], None] = time.sleep) -> str:
        while (step := mfa.step_of(datetime.now(UTC))) <= self.last_step:
            sleep(0.25)
        self.last_step = step
        return mfa.code_at(self.secret, step)

"""Two-factor authentication through the app: enrolment, the second login step, recovery
codes, the lock, the signed pre-auth state, superadmins, switching off and the CLI reset.

All accounts, keys and codes are synthetic. The clock of the web layer is frozen
(``clock``) wherever 30-second steps matter.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import admin
from app.auth import accounts, mfa
from app.auth.tokens import sign_form_token, sign_mfa_state
from app.settings import Settings
from app.web import account, auth
from app.web.auth import (
    MFA_COOKIE,
    MSG_MFA_EXPIRED,
    MSG_MFA_WRONG,
    PREAUTH_COOKIE,
    SESSION_COOKIE,
)
from tests.web.conftest import EMAIL, PASSWORD, TEST_SECRET_KEY, Accounts, LoggedIn
from tests.web.test_auth import (
    NEW_PASSWORD,
    _age_audit_log,
    _form_token,
    _login,
    _sessions_of,
)

Browser = Callable[..., TestClient]
ADMIN_EMAIL = "admin@platform.test"
_TEXT_KEY = re.compile(r'id="text-key">([A-Z2-7 ]+)</code>')
_RECOVERY_CODE = re.compile(r"<li><code>([0-9A-Z]{5}-[0-9A-Z]{5})</code></li>")


@dataclass
class Clock:
    now: datetime

    def tick(self, seconds: int = mfa.STEP_SECONDS) -> None:
        self.now += timedelta(seconds=seconds)

    def code(self, secret: bytes, steps: int = 0) -> str:
        """The code of the current step (``steps`` = -1: the previous one)."""
        return mfa.code_at(secret, mfa.step_of(self.now) + steps)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    real = datetime.now(UTC)
    start = real - timedelta(seconds=real.timestamp() % mfa.STEP_SECONDS) + timedelta(seconds=10)
    frozen = Clock(start)
    monkeypatch.setattr(auth, "utc_now", lambda: frozen.now)
    monkeypatch.setattr(account, "utc_now", lambda: frozen.now)
    return frozen


@dataclass(frozen=True)
class Enrolled:
    user_id: int
    secret: bytes
    codes: list[str]


def _secret(html: str) -> bytes:
    match = _TEXT_KEY.search(html)
    assert match, "no text key on the page"
    key = match.group(1).replace(" ", "")
    return base64.b32decode(key + "=" * (-len(key) % 8))


def enrol(c: TestClient, clock: Clock) -> tuple[bytes, list[str]]:
    """Switch 2FA on through /account/2fa; returns the secret and the recovery codes."""
    page = c.get("/account/2fa")
    assert page.status_code == 200, page.text
    secret = _secret(page.text)
    done = c.post("/account/2fa/enable", data={"code": clock.code(secret)})
    assert done.status_code == 200, done.text
    codes = _RECOVERY_CODE.findall(done.text)
    assert len(codes) == 10
    return secret, codes


@pytest.fixture
def enrolled(logged_in: LoggedIn, clock: Clock) -> Enrolled:
    """The owner (``EMAIL``/``PASSWORD``) with 2FA on; the clock is one step later."""
    with logged_in.client() as c:
        secret, codes = enrol(c, clock)
    clock.tick()  # the enrolment code's step is used
    return Enrolled(logged_in.user_id, secret, codes)


def second_step(c: TestClient, code: str, form_token: str | None = None):
    if form_token is None:
        page = c.get("/login/2fa")
        assert page.status_code == 200, page.text
        form_token = _form_token(page.text)
    return c.post("/login/2fa", data={"form_token": form_token, "code": code})


def password_step(c: TestClient, email: str = EMAIL) -> None:
    response = _login(c, email=email)
    assert response.status_code == 303
    assert response.headers["location"] == "/login/2fa"


def actions(accts: Accounts, prefix: str = "mfa") -> list[tuple]:
    rows = accts.sql(
        "SELECT action, outcome, user_id, target_user_id FROM audit_log "
        "WHERE action LIKE :p ORDER BY id",
        p=prefix + "%",
    )
    return [tuple(r) for r in rows]


def mfa_columns(accts: Accounts, user_id: int) -> tuple:
    return tuple(
        accts.sql(
            "SELECT mfa_secret IS NOT NULL, mfa_enabled_at IS NOT NULL, mfa_last_step "
            "FROM users WHERE id = :u",
            u=user_id,
        )[0]
    )


def recovery_rows(accts: Accounts, user_id: int) -> tuple[int, int]:
    """(codes, unused codes)."""
    [(total, unused)] = accts.sql(
        "SELECT count(*), count(*) FILTER (WHERE used_at IS NULL) FROM mfa_recovery_codes "
        "WHERE user_id = :u",
        u=user_id,
    )
    return total, unused


# --- Enrolment -------------------------------------------------------------------------------


def test_enrolment_needs_a_correct_code(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    page = client.get("/account/2fa")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert '<svg width="' in page.text and 'class="qr-code"' in page.text
    assert " style=" not in page.text and "<style" not in page.text  # CSP: no inline styles
    secret = _secret(page.text)
    assert mfa_columns(accts, logged_in.user_id) == (True, False, None)  # pending only

    wrong = client.post("/account/2fa/enable", data={"code": clock.code(secret, -2)})
    assert wrong.status_code == 400
    assert account.MSG_ENROL_WRONG in wrong.text
    assert _secret(wrong.text) == secret  # the same key: the app's entry still works
    assert mfa_columns(accts, logged_in.user_id) == (True, False, None)
    assert client.get("/account/2fa").text.count(mfa.text_key(secret)) == 1  # kept on reload

    done = client.post("/account/2fa/enable", data={"code": clock.code(secret)})
    assert done.status_code == 200 and done.headers["cache-control"] == "no-store"
    codes = _RECOVERY_CODE.findall(done.text)
    assert len(codes) == 10 and "only time the codes are shown" in done.text
    assert mfa_columns(accts, logged_in.user_id) == (True, True, mfa.step_of(clock.now))
    assert recovery_rows(accts, logged_in.user_id) == (10, 10)
    assert actions(accts) == [
        ("mfa_enabled", "failure", logged_in.user_id, None),
        ("mfa_enabled", "success", logged_in.user_id, None),
    ]

    after = client.get("/account/2fa")
    assert "Recovery codes left: <strong>10</strong>" in after.text
    assert 'class="qr-code"' not in after.text and not _TEXT_KEY.search(after.text)
    assert not any(code in after.text for code in codes)  # shown once only


def test_enrolment_page_never_shows_another_users_secret(
    web_app: FastAPI, client: TestClient, accts: Accounts
) -> None:
    other = accts.user("other@org.test")
    with LoggedIn(web_app, other, accts.session(other)).client() as other_client:
        theirs = _secret(other_client.get("/account/2fa").text)
    assert _secret(client.get("/account/2fa").text) != theirs


# --- The second login step -------------------------------------------------------------------


def test_login_without_2fa_has_no_second_step(browser: Browser, accts: Accounts) -> None:
    user_id = accts.user()
    c = browser()
    response = _login(c)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert MFA_COOKIE not in response.cookies
    assert len(_sessions_of(accts, user_id)) == 1


def test_login_with_2fa_needs_the_code(
    browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    accts.sql("DELETE FROM sessions RETURNING id")
    c = browser()
    response = _login(c)
    assert response.status_code == 303 and response.headers["location"] == "/login/2fa"
    state = next(h for h in response.headers.get_list("set-cookie") if h.startswith(MFA_COOKIE))
    assert "HttpOnly" in state and "SameSite=strict" in state and "Max-Age=300" in state
    assert SESSION_COOKIE not in response.cookies
    assert _sessions_of(accts, enrolled.user_id) == []  # no session before the code
    assert c.get("/").status_code == 303  # still logged out

    page = c.get("/login/2fa")
    assert page.status_code == 200 and 'autocomplete="one-time-code"' in page.text
    wrong = second_step(c, clock.code(enrolled.secret, -3))
    assert wrong.status_code == 401 and MSG_MFA_WRONG in wrong.text
    assert _sessions_of(accts, enrolled.user_id) == []

    done = second_step(c, clock.code(enrolled.secret))
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert len(_sessions_of(accts, enrolled.user_id)) == 1
    assert c.get("/").status_code == 200
    assert MFA_COOKIE not in c.cookies  # cleared
    assert actions(accts, "login") == [("login", "success", enrolled.user_id, None)]
    assert actions(accts, "mfa_login") == [
        ("mfa_login", "failure", enrolled.user_id, None),
        ("mfa_login", "success", enrolled.user_id, None),
    ]
    [(last_login,)] = accts.sql("SELECT last_login_at FROM users WHERE id = :u", u=enrolled.user_id)
    assert last_login == clock.now


def test_previous_step_is_accepted_older_is_not(
    browser: Browser, enrolled: Enrolled, clock: Clock
) -> None:
    clock.tick()  # the previous step is now later than the enrolment's
    c = browser()
    password_step(c)
    assert second_step(c, clock.code(enrolled.secret, -2)).status_code == 401
    assert second_step(c, clock.code(enrolled.secret, -1)).status_code == 303


def test_a_code_cannot_be_used_twice(browser: Browser, enrolled: Enrolled, clock: Clock) -> None:
    code = clock.code(enrolled.secret)
    first = browser()
    password_step(first)
    assert second_step(first, code).status_code == 303

    again = browser()
    password_step(again)
    replay = second_step(again, code)
    assert replay.status_code == 401 and MSG_MFA_WRONG in replay.text
    # The previous step is older than the used one: refused too.
    assert second_step(again, clock.code(enrolled.secret, -1)).status_code == 401
    clock.tick()
    assert second_step(again, clock.code(enrolled.secret)).status_code == 303


def test_recovery_code_works_once(
    browser: Browser, client: TestClient, accts: Accounts, enrolled: Enrolled
) -> None:
    code = enrolled.codes[3]
    first = browser()
    password_step(first)
    typed = code.lower().replace("-", " ")  # typed loosely
    assert second_step(first, typed).status_code == 303
    assert recovery_rows(accts, enrolled.user_id) == (10, 9)
    assert ("mfa_recovery_used", "success", enrolled.user_id, None) in actions(accts)

    again = browser()
    password_step(again)
    assert second_step(again, code).status_code == 401
    assert "Recovery codes left: <strong>9</strong>" in client.get("/account/2fa").text


def test_wrong_codes_lock_the_account_like_passwords(
    browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    sessions_before = len(_sessions_of(accts, enrolled.user_id))  # the fixture's own
    c = browser()
    password_step(c)
    for _ in range(mfa.ACCOUNT_MAX_FAILURES):
        assert second_step(c, "000000").status_code == 401
    blocked = second_step(c, clock.code(enrolled.secret))  # right, but locked
    assert blocked.status_code == 401 and MSG_MFA_WRONG in blocked.text
    assert actions(accts, "mfa_blocked") == [("mfa_blocked", "failure", enrolled.user_id, None)]
    assert len(_sessions_of(accts, enrolled.user_id)) == sessions_before

    _age_audit_log(accts, "mfa_login")  # the window has passed
    assert second_step(c, clock.code(enrolled.secret)).status_code == 303
    assert len(_sessions_of(accts, enrolled.user_id)) == sessions_before + 1


def test_recovery_codes_are_locked_too(
    browser: Browser, accts: Accounts, enrolled: Enrolled
) -> None:
    c = browser()
    password_step(c)
    for _ in range(mfa.ACCOUNT_MAX_FAILURES):
        second_step(c, "ZZZZZ-ZZZZZ")
    assert second_step(c, enrolled.codes[0]).status_code == 401
    assert recovery_rows(accts, enrolled.user_id) == (10, 10)  # not used up by the refusal


# --- The signed pre-auth state ---------------------------------------------------------------


def _forged_state(c: TestClient, user_id: int, *, age: float = 0, key: str = TEST_SECRET_KEY):
    nonce = c.cookies[PREAUTH_COOKIE]
    return sign_mfa_state(key.encode(), user_id, nonce, now=time.time() - age)


def _expired(response) -> bool:
    return response.status_code == 401 and MSG_MFA_EXPIRED in response.text


def test_second_step_without_state_starts_over(browser: Browser, enrolled: Enrolled) -> None:
    c = browser()
    page = c.get("/login/2fa")
    assert _expired(page)
    assert 'action="/login"' in page.text


@pytest.mark.parametrize("case", ["expired", "other user", "other key", "garbled"])
def test_bad_state_is_refused(
    browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock, case: str
) -> None:
    other = accts.user("other@org.test")
    c = browser()
    password_step(c)
    form_token = _form_token(c.get("/login/2fa").text)
    good = c.cookies[MFA_COOKIE]
    if case == "expired":
        state = _forged_state(c, enrolled.user_id, age=5 * 60 + 1)
    elif case == "other user":  # the id changed, the signature kept
        state = f"{other}." + good.split(".", 1)[1]
    elif case == "other key":
        state = _forged_state(c, enrolled.user_id, key="not-the-server-key-" + "x" * 30)
    else:
        state = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    c.cookies.set(MFA_COOKIE, state)
    assert _expired(c.get("/login/2fa"))
    assert _expired(second_step(c, clock.code(enrolled.secret), form_token=form_token))
    assert len(_sessions_of(accts, enrolled.user_id)) == 1  # only the fixture's own
    assert actions(accts, "mfa_login") == []  # never reached the code check


def test_a_fresh_state_just_under_five_minutes_is_accepted(
    browser: Browser, enrolled: Enrolled, clock: Clock
) -> None:
    c = browser()
    password_step(c)
    c.cookies.set(MFA_COOKIE, _forged_state(c, enrolled.user_id, age=5 * 60 - 5))
    assert second_step(c, clock.code(enrolled.secret)).status_code == 303


def test_state_does_not_work_in_another_browser(
    browser: Browser, enrolled: Enrolled, clock: Clock
) -> None:
    """The state is tied to the pre-auth nonce of the browser that entered the password."""
    c = browser()
    password_step(c)
    thief = browser()
    thief.get("/login")  # its own pre-auth cookie
    thief.cookies.set(MFA_COOKIE, c.cookies[MFA_COOKIE])
    assert _expired(thief.get("/login/2fa"))


def test_second_step_needs_its_form_token(
    browser: Browser, enrolled: Enrolled, clock: Clock
) -> None:
    c = browser()
    password_step(c)
    code = clock.code(enrolled.secret)
    for token in (None, "", "garbage", "login"):
        data = {"code": code}
        if token == "login":  # a valid token for another form
            data["form_token"] = sign_form_token(
                TEST_SECRET_KEY.encode(), auth.LOGIN_PURPOSE, c.cookies[PREAUTH_COOKIE]
            )
        elif token is not None:
            data["form_token"] = token
        response = c.post("/login/2fa", data=data)
        assert response.status_code == 403, token
    assert second_step(c, code).status_code == 303  # nothing was used up


def test_disabled_user_cannot_finish_the_second_step(
    browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    c = browser()
    password_step(c)
    form_token = _form_token(c.get("/login/2fa").text)
    accts.sql("UPDATE users SET disabled = true WHERE id = :u RETURNING id", u=enrolled.user_id)
    assert _expired(second_step(c, clock.code(enrolled.secret), form_token=form_token))


# --- Superadmins -----------------------------------------------------------------------------


@pytest.fixture
def new_admin(web_app: FastAPI, accts: Accounts) -> LoggedIn:
    """A superadmin who has not set up 2FA yet, with a session."""
    user_id = accts.user(ADMIN_EMAIL, superadmin=True, mfa=False)
    return LoggedIn(web_app, user_id, accts.session(user_id))


def test_superadmin_must_enrol_before_anything_else(
    new_admin: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    with new_admin.client(follow_redirects=False) as c:
        for path in ("/", "/history", "/admin/orgs", "/admin/requests", "/template.xlsx"):
            response = c.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/account/2fa", path
        htmx = c.get("/history", headers={"HX-Request": "true"})
        assert htmx.status_code == 401 and htmx.headers["hx-redirect"] == "/account/2fa"
        post = c.post("/validate", files={"file": ("v.csv", b"iccid,activation_code\n")})
        assert post.status_code == 303 and post.headers["location"] == "/account/2fa"

        page = c.get("/account/2fa")
        assert page.status_code == 200
        assert "Superadmins must use two-factor authentication" in page.text
        enrol(c, clock)
        assert c.get("/admin/orgs").status_code == 200
        on = c.get("/account/2fa")
        assert "Superadmins cannot switch two-factor authentication off" in on.text
        assert 'action="/account/2fa/disable"' not in on.text


def test_superadmin_without_2fa_can_log_out(new_admin: LoggedIn, accts: Accounts) -> None:
    with new_admin.client(follow_redirects=False) as c:
        assert c.post("/logout").status_code == 303
    assert _sessions_of(accts, new_admin.user_id) == []


def test_superadmin_cannot_switch_2fa_off(
    new_admin: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    with new_admin.client(follow_redirects=False) as c:
        secret, codes = enrol(c, clock)
        clock.tick()
        refused = c.post("/account/2fa/disable", data={"code": clock.code(secret)})
        assert refused.status_code == 403
        assert account.MSG_SUPERADMIN_KEEPS in refused.text
        assert c.post("/account/2fa/disable", data={"code": codes[0]}).status_code == 403
    assert mfa_columns(accts, new_admin.user_id)[:2] == (True, True)
    assert recovery_rows(accts, new_admin.user_id) == (10, 10)


def test_superadmin_logs_in_with_the_second_step(
    browser: Browser, new_admin: LoggedIn, clock: Clock
) -> None:
    with new_admin.client() as c:
        secret, _ = enrol(c, clock)
    clock.tick()
    b = browser()
    password_step(b, ADMIN_EMAIL)
    assert second_step(b, clock.code(secret)).status_code == 303
    assert b.get("/admin/orgs").status_code == 200


# --- Switching off ---------------------------------------------------------------------------


def test_optional_user_can_switch_2fa_off(
    browser: Browser, client: TestClient, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    page = client.get("/account/2fa")
    assert 'action="/account/2fa/disable"' in page.text
    wrong = client.post("/account/2fa/disable", data={"code": "000000"})
    assert wrong.status_code == 400 and MSG_MFA_WRONG in wrong.text
    assert mfa_columns(accts, enrolled.user_id)[:2] == (True, True)

    done = client.post(
        "/account/2fa/disable", data={"code": clock.code(enrolled.secret)}, follow_redirects=False
    )
    assert done.status_code == 303 and done.headers["location"] == "/account/2fa?off=1"
    assert mfa_columns(accts, enrolled.user_id) == (False, False, None)
    assert recovery_rows(accts, enrolled.user_id) == (0, 0)
    assert actions(accts)[-2:] == [
        ("mfa_disabled", "failure", enrolled.user_id, None),
        ("mfa_disabled", "success", enrolled.user_id, None),
    ]
    assert "Two-factor authentication is off." in client.get("/account/2fa?off=1").text

    c = browser()
    response = _login(c)  # no second step any more
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_switching_off_with_a_recovery_code(
    client: TestClient, accts: Accounts, enrolled: Enrolled
) -> None:
    response = client.post(
        "/account/2fa/disable", data={"code": enrolled.codes[9]}, follow_redirects=False
    )
    assert response.status_code == 303
    assert mfa_columns(accts, enrolled.user_id) == (False, False, None)


def test_wrong_codes_on_the_off_form_count_towards_the_lock(
    browser: Browser, client: TestClient, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    for _ in range(mfa.ACCOUNT_MAX_FAILURES):
        assert client.post("/account/2fa/disable", data={"code": "000000"}).status_code == 400
    blocked = client.post("/account/2fa/disable", data={"code": clock.code(enrolled.secret)})
    assert blocked.status_code == 400
    assert mfa_columns(accts, enrolled.user_id)[:2] == (True, True)
    c = browser()
    password_step(c)
    assert second_step(c, clock.code(enrolled.secret)).status_code == 401  # same lock


# --- Secrets never stored or logged in plaintext ---------------------------------------------


def test_secret_and_codes_never_in_the_database_or_logs(
    browser: Browser,
    logged_in: LoggedIn,
    accts: Accounts,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    with logged_in.client() as c:
        secret, codes = enrol(c, clock)
    clock.tick()
    b = browser()
    password_step(b)
    used = clock.code(secret)
    assert second_step(b, used).status_code == 303
    b2 = browser()
    password_step(b2)
    assert second_step(b2, codes[0]).status_code == 303

    key = base64.b32encode(secret).decode().rstrip("=")
    forbidden = [
        key,
        mfa.text_key(secret),
        secret.hex(),
        base64.b64encode(secret).decode(),
        *codes,
        *(code.replace("-", "") for code in codes),
    ]
    logs = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    for value in forbidden:
        assert value not in logs, "a secret or recovery code was logged"

    tables = [
        name
        for (name,) in accts.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
    ]
    dump = "\n".join(
        row[0]
        for table in tables
        for row in accts.sql(f"SELECT row_to_json(t)::text FROM {table} t")  # noqa: S608
    )
    for value in forbidden:
        assert value not in dump and value.lower() not in dump
    [(stored,)] = accts.sql("SELECT mfa_secret FROM users WHERE id = :u", u=logged_in.user_id)
    assert secret not in bytes(stored)
    assert len(bytes(stored)) == 1 + 12 + 20 + 16  # version, nonce, ciphertext, tag


# --- Break-glass: python -m app.admin reset-2fa ----------------------------------------------


@pytest.fixture
def cli(
    accts: Accounts, web_settings: Settings, web_sessions: sessionmaker[Session]
) -> Iterator[Callable[..., tuple[int, str, str]]]:
    def _run(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = admin.main(
            list(argv), session_scope=web_sessions.begin, settings=web_settings, out=out, err=err
        )
        return code, out.getvalue().strip(), err.getvalue().strip()

    yield _run


def test_cli_reset_2fa(
    cli, browser: Browser, new_admin: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    with new_admin.client() as c:
        enrol(c, clock)
    assert len(_sessions_of(accts, new_admin.user_id)) == 1

    code, out, err = cli("reset-2fa", "--email", ADMIN_EMAIL.upper())
    assert (code, err) == (0, "")
    assert out == "two-factor authentication switched off; their sessions have ended"
    assert mfa_columns(accts, new_admin.user_id) == (False, False, None)
    assert recovery_rows(accts, new_admin.user_id) == (0, 0)
    assert _sessions_of(accts, new_admin.user_id) == []
    assert actions(accts, "mfa_reset") == [("mfa_reset", "success", None, new_admin.user_id)]

    # The next login has no second step, and the superadmin must enrol again.
    b = browser()
    response = _login(b, email=ADMIN_EMAIL)
    assert response.status_code == 303 and response.headers["location"] == "/"
    first = b.get("/admin/orgs")
    assert first.status_code == 303 and first.headers["location"] == "/account/2fa"

    assert cli("reset-2fa", "--email", "nobody@org.test")[0] == 1


def test_cli_reset_needs_no_password_or_key(cli, accts: Accounts) -> None:
    """Works for a user whose 2FA cannot be read any more (e.g. MFA_KEY changed)."""
    user_id = accts.user("member@org.test", mfa=True)  # placeholder secret: unreadable
    assert cli("reset-2fa", "--email", "member@org.test")[0] == 0
    assert mfa_columns(accts, user_id) == (False, False, None)


def test_password_still_needed_with_2fa(browser: Browser, enrolled: Enrolled) -> None:
    c = browser()
    response = _login(c, email=EMAIL, password=PASSWORD + "x")
    assert response.status_code == 401
    assert MFA_COOKIE not in response.cookies
    assert _expired(c.get("/login/2fa"))


# --- A new password never changes 2FA --------------------------------------------------------


def set_password_via_link(c: TestClient, token: str, password: str = NEW_PASSWORD):
    page = c.get(f"/invite/{token}")
    assert page.status_code == 200, page.text
    return c.post(
        f"/invite/{token}",
        data={
            "form_token": _form_token(page.text),
            "password": password,
            "password_confirm": password,
        },
    )


def _assert_link_keeps_2fa(
    browser: Browser, token: str, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    """Setting the password from ``token`` leaves 2FA as it was and still needs the code."""
    c = browser()
    before = mfa_columns(accts, enrolled.user_id)
    response = set_password_via_link(c, token)
    assert response.status_code == 303
    assert response.headers["location"] == "/login/2fa"  # not a session
    assert SESSION_COOKIE not in response.cookies
    assert _sessions_of(accts, enrolled.user_id) == []  # the link ended the others, too
    assert mfa_columns(accts, enrolled.user_id) == before
    assert recovery_rows(accts, enrolled.user_id) == (10, 10)
    assert actions(accts, "mfa_") == [("mfa_enabled", "success", enrolled.user_id, None)]

    assert second_step(c, clock.code(enrolled.secret)).status_code == 303
    assert len(_sessions_of(accts, enrolled.user_id)) == 1

    response = _login(browser(), password=NEW_PASSWORD)  # the new password: still two steps
    assert response.status_code == 303 and response.headers["location"] == "/login/2fa"


def test_reset_link_keeps_2fa_and_needs_the_second_step(
    browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    """A set-password link (an administrator's or owner's "Reset link") never touches 2FA."""
    token = accts.run(lambda db: accounts.reset_link(db, EMAIL, now=clock.now))
    _assert_link_keeps_2fa(browser, token, accts, enrolled, clock)


def test_cli_reset_link_keeps_2fa_and_needs_the_second_step(
    cli, browser: Browser, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    code, out, _ = cli("reset-link", "--email", EMAIL)
    assert code == 0
    token = out.rsplit("/invite/", 1)[1]
    _assert_link_keeps_2fa(browser, token, accts, enrolled, clock)


def test_new_password_for_a_superadmin_with_2fa_needs_the_code(
    cli, browser: Browser, new_admin: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    with new_admin.client() as c:
        secret, _ = enrol(c, clock)
    clock.tick()
    token = cli("reset-link", "--email", ADMIN_EMAIL)[1].rsplit("/invite/", 1)[1]
    b = browser()
    response = set_password_via_link(b, token)
    assert response.headers["location"] == "/login/2fa"
    assert mfa_columns(accts, new_admin.user_id)[:2] == (True, True)
    assert second_step(b, clock.code(secret)).status_code == 303
    assert b.get("/admin/orgs").status_code == 200


# --- Generate new recovery codes -------------------------------------------------------------


def regenerate(c: TestClient, code: str):
    return c.post("/account/2fa/recovery-codes", data={"code": code})


def test_new_recovery_codes_need_a_current_code_and_replace_the_old(
    browser: Browser, client: TestClient, accts: Accounts, enrolled: Enrolled, clock: Clock
) -> None:
    page = client.get("/account/2fa")
    assert 'action="/account/2fa/recovery-codes"' in page.text

    wrong = regenerate(client, "000000")
    assert wrong.status_code == 400 and account.MSG_REGENERATE_WRONG in wrong.text
    recovery_instead = regenerate(client, enrolled.codes[0])  # a current TOTP code only
    assert recovery_instead.status_code == 400
    assert recovery_rows(accts, enrolled.user_id) == (10, 10)  # nothing used or replaced

    used = clock.code(enrolled.secret)
    done = regenerate(client, used)
    assert done.status_code == 200 and done.headers["cache-control"] == "no-store"
    assert "New recovery codes" in done.text and "old recovery codes no longer work" in done.text
    new_codes = _RECOVERY_CODE.findall(done.text)
    assert len(new_codes) == 10 and set(new_codes).isdisjoint(enrolled.codes)
    assert recovery_rows(accts, enrolled.user_id) == (10, 10)
    assert actions(accts, "mfa_recovery_regenerated") == [
        ("mfa_recovery_regenerated", "failure", enrolled.user_id, None),
        ("mfa_recovery_regenerated", "failure", enrolled.user_id, None),
        ("mfa_recovery_regenerated", "success", enrolled.user_id, None),
    ]
    after = client.get("/account/2fa").text
    assert not any(code in after for code in new_codes)  # shown once only

    replay = browser()
    password_step(replay)
    assert second_step(replay, used).status_code == 401  # that TOTP code is used up

    old = browser()
    password_step(old)
    assert second_step(old, enrolled.codes[1]).status_code == 401  # old codes are gone
    fresh = browser()
    password_step(fresh)
    assert second_step(fresh, new_codes[0]).status_code == 303


def test_regenerating_needs_2fa_on(client: TestClient, accts: Accounts, logged_in: LoggedIn):
    response = client.post(
        "/account/2fa/recovery-codes", data={"code": "123456"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/account/2fa"
    assert recovery_rows(accts, logged_in.user_id) == (0, 0)


def test_superadmin_can_regenerate_recovery_codes(
    new_admin: LoggedIn, accts: Accounts, clock: Clock
) -> None:
    with new_admin.client() as c:
        secret, first = enrol(c, clock)
        clock.tick()
        done = regenerate(c, clock.code(secret))
    assert done.status_code == 200
    assert set(_RECOVERY_CODE.findall(done.text)).isdisjoint(first)
    assert recovery_rows(accts, new_admin.user_id) == (10, 10)


def test_wrong_codes_on_regenerate_count_towards_the_lock(
    browser: Browser, client: TestClient, enrolled: Enrolled, clock: Clock
) -> None:
    for _ in range(mfa.ACCOUNT_MAX_FAILURES):
        assert regenerate(client, "000000").status_code == 400
    assert regenerate(client, clock.code(enrolled.secret)).status_code == 400  # locked
    c = browser()
    password_step(c)
    assert second_step(c, clock.code(enrolled.secret)).status_code == 401


# --- Two second steps at once with the same code ---------------------------------------------


def test_two_simultaneous_second_steps_with_one_code(
    browser: Browser,
    accts: Accounts,
    enrolled: Enrolled,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests with the same code at the same moment: exactly one logs in.

    Both requests are made to meet right after reading the user row. With the row lock
    (``mfa._locked_user``, ``FOR UPDATE``) the second cannot read it until the first has
    committed, so they never meet: the first goes on after the barrier's timeout, and the
    second then sees the code's step as used. Without the lock both would read the same
    state, meet, and both log in (checked by hand: 303 and 303)."""
    code = clock.code(enrolled.secret)
    clients = [browser(), browser()]
    form_tokens = []
    for c in clients:
        password_step(c)
        form_tokens.append(_form_token(c.get("/login/2fa").text))
    sessions_before = len(_sessions_of(accts, enrolled.user_id))

    meet = threading.Barrier(2, timeout=2)
    real_locked_user = mfa._locked_user
    met: list[bool] = []

    def lock_then_meet(db, user_id):
        user = real_locked_user(db, user_id)
        try:
            meet.wait()  # only reached by both if the row was not locked
            met.append(True)
        except threading.BrokenBarrierError:
            pass  # the other request is still waiting for the row lock
        return user

    monkeypatch.setattr(mfa, "_locked_user", lock_then_meet)
    responses: list = [None, None]

    def post(i: int) -> None:
        responses[i] = clients[i].post(
            "/login/2fa", data={"form_token": form_tokens[i], "code": code}
        )

    threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r is not None for r in responses), "a request did not finish"
    assert met == [], "both requests read the user row at once: it was not locked"
    assert sorted(r.status_code for r in responses) == [303, 401]
    assert len(_sessions_of(accts, enrolled.user_id)) == sessions_before + 1
    assert sorted(actions(accts, "mfa_login")) == [
        ("mfa_login", "failure", enrolled.user_id, None),
        ("mfa_login", "success", enrolled.user_id, None),
    ]

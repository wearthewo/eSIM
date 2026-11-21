"""``python -m app.admin`` against the test database; printed links work in the app.

All data is synthetic.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import admin
from app.settings import Settings
from tests.web.conftest import EMAIL, PASSWORD, Accounts

LINK = re.compile(r"http://testserver/invite/([A-Za-z0-9_-]{43})")
NEW_PASSWORD = "a brand new synthetic passphrase"
_FORM_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


@pytest.fixture
def cli(
    accts: Accounts, web_settings: Settings, web_sessions: sessionmaker[Session]
) -> Callable[..., tuple[int, str, str]]:
    """``cli("invite", "--org-id", "1", ...) -> (exit code, stdout, stderr)``."""

    def _run(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = admin.main(
            list(argv), session_scope=web_sessions.begin, settings=web_settings, out=out, err=err
        )
        return code, out.getvalue().strip(), err.getvalue().strip()

    return _run


def _token(output: str) -> str:
    match = LINK.fullmatch(output)
    assert match, output
    return match.group(1)


def _use_link(c: TestClient, token: str) -> int:
    page = c.get(f"/invite/{token}")
    if page.status_code != 200:
        return page.status_code
    form_token = _FORM_TOKEN.search(page.text).group(1)
    return c.post(
        f"/invite/{token}",
        data={"form_token": form_token, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    ).status_code


def _login(c: TestClient, email: str, password: str) -> int:
    form_token = _FORM_TOKEN.search(c.get("/login").text).group(1)
    data = {"form_token": form_token, "email": email, "password": password}
    return c.post("/login", data=data).status_code


def test_create_org_and_invite(cli, browser, accts: Accounts) -> None:
    code, out, _ = cli("create-org", "--name", "  Example Org (test) ")
    assert code == 0 and out.isdigit()
    [(name,)] = accts.sql("SELECT name FROM organizations WHERE id = :i", i=int(out))
    assert name == "Example Org (test)"

    code, link, _ = cli(
        "invite", "--org-id", out, "--email", "New.Person@Org.Test", "--role", "owner"
    )
    assert code == 0
    token = _token(link)
    assert accts.sql("SELECT email, role, password_hash FROM users") == [
        ("new.person@org.test", "owner", None)
    ]
    assert accts.sql("SELECT token_hash FROM invites")[0][0] != token.encode()  # only the hash
    assert _use_link(browser(), token) == 303
    assert _use_link(browser(), token) == 404  # single use
    assert _login(browser(), "new.person@org.test", NEW_PASSWORD) == 303


def test_create_superadmin(cli, browser, accts: Accounts) -> None:
    code, link, _ = cli("create-superadmin", "--email", "admin@org.test")
    assert code == 0
    assert accts.sql("SELECT is_superadmin, organization_id FROM users") == [(True, None)]
    c = browser()
    assert _use_link(c, _token(link)) == 303
    first_page = c.get("/")  # a superadmin must set up two-factor authentication first
    assert first_page.status_code == 303 and first_page.headers["location"] == "/account/2fa"
    assert c.get("/account/2fa").status_code == 200

    code, _, err = cli("create-superadmin", "--email", "ADMIN@org.test")
    assert code == 1 and "already exists" in err


def test_reset_link(cli, browser, accts: Accounts) -> None:
    accts.user()
    code, link, _ = cli("reset-link", "--email", EMAIL.upper())
    assert code == 0
    assert _use_link(browser(), _token(link)) == 303
    assert _login(browser(), EMAIL, PASSWORD) == 401
    assert _login(browser(), EMAIL, NEW_PASSWORD) == 303

    # A second link replaces the first.
    first = _token(cli("reset-link", "--email", EMAIL)[1])
    second = _token(cli("reset-link", "--email", EMAIL)[1])
    assert _use_link(browser(), first) == 404
    assert _use_link(browser(), second) == 303


def test_disable_and_enable_user(cli, browser, logged_in, accts: Accounts) -> None:
    session_client = logged_in.client(follow_redirects=False)
    assert session_client.get("/").status_code == 200

    code, _, _ = cli("disable-user", "--email", EMAIL)
    assert code == 0
    assert accts.sql("SELECT count(*) FROM sessions") == [(0,)]  # sessions deleted
    assert session_client.get("/").status_code == 303
    assert _login(browser(), EMAIL, PASSWORD) == 401
    code, _, err = cli("reset-link", "--email", EMAIL)
    assert code == 1 and "disabled" in err

    assert cli("enable-user", "--email", EMAIL)[0] == 0
    assert _login(browser(), EMAIL, PASSWORD) == 303


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ("invite", "--org-id", "999", "--email", "a@org.test", "--role", "member"),
            "no organization",
        ),
        (
            ("invite", "--org-id", "1", "--email", "not-an-email", "--role", "member"),
            "not a valid email",
        ),
        (
            ("invite", "--org-id", "1", "--email", EMAIL, "--role", "member"),
            "already has a password",
        ),
        (("reset-link", "--email", "nobody@org.test"), "no user"),
        (("disable-user", "--email", "nobody@org.test"), "no user"),
        (("create-org", "--name", "   "), "organization name"),
    ],
)
def test_errors(cli, accts: Accounts, argv: tuple[str, ...], message: str) -> None:
    accts.user()  # organization 1 with EMAIL, who has a password
    code, out, err = cli(*argv)
    assert code == 1 and out == ""
    assert err.startswith("error: ") and message in err


def test_invite_to_disabled_org_refused(cli, accts: Accounts) -> None:
    org_id = accts.org(disabled=True)
    code, _, err = cli(
        "invite", "--org-id", str(org_id), "--email", "a@org.test", "--role", "member"
    )
    assert code == 1 and "disabled" in err


def test_bad_role_is_rejected_by_argparse(cli, accts: Accounts) -> None:
    with pytest.raises(SystemExit):
        cli("invite", "--org-id", "1", "--email", "a@org.test", "--role", "admin")

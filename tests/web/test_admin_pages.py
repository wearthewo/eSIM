"""Superadmin pages: access requests, organizations, users. All data is synthetic."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.orm import Session

from app.db.models import AccessRequest, Role, VolumeBand
from app.web.auth import CSRF_HEADER, MSG_FORBIDDEN
from app.web.templating import TEMPLATES_DIR, templates
from tests.web.conftest import Accounts, LoggedIn
from tests.web.management_routes import (
    ADMIN_GETS,
    ADMIN_POSTS,
    NEW_PASSWORD,
    app_routes,
    audit_actions,
    count,
    fill,
    form_token,
    link_token,
    use_link,
)

Browser = Callable[..., TestClient]
ADMIN_EMAIL = "admin@org.test"


@pytest.fixture
def superadmin(web_app: FastAPI, accts: Accounts) -> LoggedIn:
    user_id = accts.user(ADMIN_EMAIL, superadmin=True, role=Role.MEMBER)
    return LoggedIn(web_app, user_id, accts.session(user_id))


@pytest.fixture
def admin_client(superadmin: LoggedIn) -> TestClient:
    return superadmin.client(follow_redirects=False)


def add_request(
    accts: Accounts, email: str = "applicant@org.test", company: str = "Example Telecom (test)"
) -> int:
    def _add(db: Session) -> int:
        request = AccessRequest(
            company_name=company,
            contact_name="Test Applicant",
            email=email,
            volume_band=VolumeBand.UNDER_1K,
            note=None,
        )
        db.add(request)
        db.flush()
        return request.id

    return accts.run(_add)


def status_of(accts: Accounts, request_id: int) -> tuple:
    row = accts.sql(
        "SELECT status, handled_by, handled_at IS NOT NULL FROM access_requests WHERE id = :i",
        i=request_id,
    )[0]
    return tuple(row)


# --- 404 for everyone but superadmins ---------------------------------------------------------


def test_admin_routes_are_the_known_ones(web_app: FastAPI) -> None:
    expected = [("GET", p) for p in ADMIN_GETS] + [("POST", p) for p in ADMIN_POSTS]
    assert app_routes(web_app, "/admin") == sorted(expected)


@pytest.mark.parametrize("role", [Role.OWNER, Role.MEMBER])
def test_non_superadmin_gets_404_on_every_admin_route(
    web_app: FastAPI, accts: Accounts, role: Role
) -> None:
    org_id = accts.org("Target Org (test)")
    victim = accts.user("victim@org.test", org_id=org_id, role=Role.MEMBER)
    request_id = add_request(accts)
    caller = accts.user(f"{role.value}@org.test", role=role)  # in its own organization
    c = LoggedIn(web_app, caller, accts.session(caller)).client(follow_redirects=False)
    unknown = c.get("/no-such-page")
    assert unknown.status_code == 404

    routes = app_routes(web_app, "/admin")
    assert routes  # enumerated from the app, not from a list kept by hand
    for method, path in routes:
        url = fill(path, org_id=org_id, user_id=victim, request_id=request_id)
        data = {"daily_row_quota": "1", "email": "new@org.test", "role": "owner"}
        response = c.request(method, url, data=data if method == "POST" else None)
        assert response.status_code == 404, (method, path)
        assert response.content == unknown.content, (method, path)

    assert status_of(accts, request_id) == ("pending", None, False)
    org = accts.sql("SELECT disabled, daily_row_quota FROM organizations WHERE id = :i", i=org_id)
    assert tuple(org[0]) == (False, 20_000)
    assert accts.sql("SELECT disabled FROM users WHERE id = :i", i=victim)[0][0] is False
    assert count(accts, "invites") == count(accts, "password_tokens") == 0
    assert count(accts, "organizations") == 2
    assert count(accts, "audit_log") == 0


def test_logged_out_admin_route_goes_to_login(anon: TestClient) -> None:
    response = anon.get("/admin/orgs")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("token", [None, "wrong"])
def test_csrf_required_on_every_admin_post(
    superadmin: LoggedIn, accts: Accounts, token: str | None
) -> None:
    org_id = accts.org()
    user_id = accts.user("member@org.test", org_id=org_id, role=Role.MEMBER)
    request_id = add_request(accts)
    c = superadmin.client(follow_redirects=False)
    del c.headers[CSRF_HEADER]
    for path in ADMIN_POSTS:
        url = fill(path, org_id=org_id, user_id=user_id, request_id=request_id)
        data = {"daily_row_quota": "5", "email": "x@org.test", "role": "member"}
        if token is not None:
            data["csrf_token"] = token
        response = c.post(url, data=data)
        assert response.status_code == 403, path
        assert response.text == MSG_FORBIDDEN
    assert status_of(accts, request_id) == ("pending", None, False)
    assert count(accts, "audit_log") == 0
    assert count(accts, "invites") == count(accts, "password_tokens") == 0


def test_csrf_form_field_is_accepted(superadmin: LoggedIn, accts: Accounts) -> None:
    request_id = add_request(accts)
    c = superadmin.client(follow_redirects=False)
    del c.headers[CSRF_HEADER]
    response = c.post(f"/admin/requests/{request_id}/reject", data={"csrf_token": superadmin.csrf})
    assert response.status_code == 303


# --- Header -----------------------------------------------------------------------------------


def test_header_shows_admin_links_to_superadmins_only(
    admin_client: TestClient, client: TestClient, accts: Accounts
) -> None:
    add_request(accts, "one@org.test")
    add_request(accts, "two@org.test")
    page = admin_client.get("/").text
    assert 'href="/admin/orgs">Admin</a>' in page
    assert "Requests (2)" in page
    owner_page = client.get("/").text
    assert "/admin" not in owner_page
    assert "Requests (" not in owner_page


def test_admin_home_redirects(admin_client: TestClient) -> None:
    response = admin_client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/orgs"


# --- Requests ---------------------------------------------------------------------------------


def test_requests_page_lists_pending_newest_first(
    admin_client: TestClient, accts: Accounts
) -> None:
    first = add_request(accts, "older@org.test", "Older Co (test)")
    add_request(accts, "newer@org.test", "Newer Co (test)")
    accts.sql(
        "UPDATE access_requests SET created_at = created_at - interval '1 day' WHERE id = :i "
        "RETURNING id",
        i=first,
    )
    page = admin_client.get("/admin/requests").text
    assert page.index("newer@org.test") < page.index("older@org.test")
    assert "Under 1,000" in page
    assert 'href="/admin/requests?show=handled"' in page


def test_approve_creates_org_owner_invite_once(
    superadmin: LoggedIn, admin_client: TestClient, accts: Accounts, browser: Browser
) -> None:
    request_id = add_request(accts, "Applicant@Org.test", "Example Telecom (test)")
    response = admin_client.post(f"/admin/requests/{request_id}/approve")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'data-copy-target="one-time-link"' in response.text
    token = link_token(response.text)

    org_id, name = accts.sql("SELECT id, name FROM organizations")[0]
    assert name == "Example Telecom (test)"
    owner = accts.sql(
        "SELECT id, email, role, organization_id, password_hash FROM users WHERE NOT is_superadmin"
    )
    assert len(owner) == 1
    owner_id, email, role, owner_org, password_hash = owner[0]
    assert (email, role, owner_org, password_hash) == ("applicant@org.test", "owner", org_id, None)
    assert count(accts, "invites", "role = 'owner' AND organization_id = :o", o=org_id) == 1
    assert status_of(accts, request_id) == ("approved", superadmin.user_id, True)
    assert audit_actions(accts) == [
        ("access_request_approved", "success", superadmin.user_id, owner_id, org_id),
        ("invite_created", "success", superadmin.user_id, owner_id, org_id),
    ]

    # The link works, and only once.
    assert use_link(browser(), token) == 303
    assert use_link(browser(), token) == 404

    again = admin_client.post(f"/admin/requests/{request_id}/approve")
    assert again.status_code == 409
    assert "already been handled" in again.text
    assert count(accts, "organizations") == 1
    assert count(accts, "invites") == 1


def test_approve_with_existing_users_email_creates_nothing(
    admin_client: TestClient, accts: Accounts
) -> None:
    accts.user("taken@org.test")  # creates one organization
    request_id = add_request(accts, "TAKEN@org.test")
    response = admin_client.post(f"/admin/requests/{request_id}/approve")
    assert response.status_code == 409
    assert "TAKEN@org.test already has an account" in response.text  # as stored
    assert count(accts, "organizations") == 1
    assert count(accts, "users", "NOT is_superadmin") == 1
    assert count(accts, "invites") == 0
    assert status_of(accts, request_id) == ("pending", None, False)
    assert [a[:2] for a in audit_actions(accts)] == [("access_request_approved", "failure")]


def test_reject(superadmin: LoggedIn, admin_client: TestClient, accts: Accounts) -> None:
    request_id = add_request(accts)
    response = admin_client.post(f"/admin/requests/{request_id}/reject")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/requests"
    assert status_of(accts, request_id) == ("rejected", superadmin.user_id, True)
    assert count(accts, "organizations") == count(accts, "invites") == 0
    assert audit_actions(accts) == [
        ("access_request_rejected", "success", superadmin.user_id, None, None)
    ]
    assert "applicant@org.test" not in admin_client.get("/admin/requests").text
    handled = admin_client.get("/admin/requests?show=handled").text
    assert "applicant@org.test" in handled
    assert f"by {ADMIN_EMAIL}" in handled

    assert admin_client.post(f"/admin/requests/{request_id}/reject").status_code == 409
    assert admin_client.post(f"/admin/requests/{request_id}/approve").status_code == 409


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_unknown_request_is_404(admin_client: TestClient, action: str) -> None:
    assert admin_client.post(f"/admin/requests/999/{action}").status_code == 404
    assert admin_client.post(f"/admin/requests/0/{action}").status_code == 422
    assert admin_client.post(f"/admin/requests/{2**63}/{action}").status_code == 422


# --- Organizations ------------------------------------------------------------------------------


def test_org_list_and_detail(admin_client: TestClient, accts: Accounts) -> None:
    org_id = accts.org("Listed Org (test)")
    accts.user("owner@org.test", org_id=org_id, role=Role.OWNER)
    accts.user("member@org.test", org_id=org_id, role=Role.MEMBER, password_hash=None)
    today = datetime.now(UTC).date()
    accts.sql(
        "INSERT INTO usage (organization_id, day, rows_generated) VALUES (:o, :d, 1234) "
        "RETURNING organization_id",
        o=org_id,
        d=today,
    )
    page = admin_client.get("/admin/orgs").text
    assert f'href="/admin/orgs/{org_id}">Listed Org (test)</a>' in page
    assert "<td>2</td>" in page
    assert "1,234" in page
    assert "20,000" in page

    detail = admin_client.get(f"/admin/orgs/{org_id}").text
    assert "owner@org.test" in detail
    assert "member@org.test" in detail
    assert "invited, no password yet" in detail
    assert admin_client.get("/admin/orgs/999").status_code == 404


def test_quota_change(superadmin: LoggedIn, admin_client: TestClient, accts: Accounts) -> None:
    org_id = accts.org()
    response = admin_client.post(f"/admin/orgs/{org_id}/quota", data={"daily_row_quota": "5000"})
    assert response.status_code == 303
    quota = "SELECT daily_row_quota FROM organizations WHERE id = :i"
    assert accts.sql(quota, i=org_id)[0][0] == 5000
    assert audit_actions(accts) == [("quota_changed", "success", superadmin.user_id, None, org_id)]
    for bad in ["-1", "abc", "10000001", "", "1e3", "٣"]:
        response = admin_client.post(f"/admin/orgs/{org_id}/quota", data={"daily_row_quota": bad})
        assert response.status_code == 400, bad
    assert accts.sql(quota, i=org_id)[0][0] == 5000
    assert (
        admin_client.post("/admin/orgs/999/quota", data={"daily_row_quota": "1"}).status_code == 404
    )


def test_disable_org_ends_sessions(
    superadmin: LoggedIn, admin_client: TestClient, logged_in: LoggedIn, accts: Accounts
) -> None:
    owner = logged_in.client(follow_redirects=False)
    assert owner.get("/").status_code == 200
    org_id = accts.sql("SELECT organization_id FROM users WHERE id = :i", i=logged_in.user_id)[0][0]

    assert admin_client.post(f"/admin/orgs/{org_id}/disable").status_code == 303
    assert count(accts, "sessions", "user_id = :u", u=logged_in.user_id) == 0
    assert owner.get("/").status_code == 303
    assert accts.sql("SELECT disabled FROM organizations WHERE id = :i", i=org_id)[0][0] is True

    assert admin_client.post(f"/admin/orgs/{org_id}/enable").status_code == 303
    assert accts.sql("SELECT disabled FROM organizations WHERE id = :i", i=org_id)[0][0] is False
    assert [a[0] for a in audit_actions(accts)] == ["org_disabled", "org_enabled"]


def test_disable_user_ends_sessions(
    superadmin: LoggedIn, admin_client: TestClient, logged_in: LoggedIn, accts: Accounts
) -> None:
    user = logged_in.client(follow_redirects=False)
    assert user.get("/").status_code == 200
    response = admin_client.post(f"/admin/users/{logged_in.user_id}/disable")
    assert response.status_code == 303
    assert count(accts, "sessions", "user_id = :u", u=logged_in.user_id) == 0
    assert user.get("/").status_code == 303

    assert admin_client.post(f"/admin/users/{logged_in.user_id}/enable").status_code == 303
    assert accts.sql("SELECT disabled FROM users WHERE id = :i", i=logged_in.user_id)[0][0] is False
    actions = audit_actions(accts)
    assert [(a[0], a[2], a[3]) for a in actions] == [
        ("user_disabled", superadmin.user_id, logged_in.user_id),
        ("user_enabled", superadmin.user_id, logged_in.user_id),
    ]


def test_superadmin_cannot_act_on_self(superadmin: LoggedIn, admin_client: TestClient) -> None:
    for action in ("disable", "enable", "reset-link"):
        response = admin_client.post(f"/admin/users/{superadmin.user_id}/{action}")
        assert response.status_code == 404
    assert admin_client.post("/admin/users/999/disable").status_code == 404


def test_admin_invites_owner_or_member(
    admin_client: TestClient, accts: Accounts, browser: Browser
) -> None:
    org_id = accts.org()
    for email, role in (("new.owner@org.test", "owner"), ("new.member@org.test", "member")):
        response = admin_client.post(
            f"/admin/orgs/{org_id}/invite", data={"email": email, "role": role}
        )
        assert response.status_code == 200
        token = link_token(response.text)
        assert accts.sql("SELECT role FROM users WHERE email = :e", e=email)[0][0] == role
        assert use_link(browser(), token) == 303
    bad = admin_client.post(
        f"/admin/orgs/{org_id}/invite", data={"email": "x@org.test", "role": "boss"}
    )
    assert bad.status_code == 400
    taken = admin_client.post(
        f"/admin/orgs/{org_id}/invite", data={"email": "new.owner@org.test", "role": "member"}
    )
    assert taken.status_code == 400
    assert "already has an account" in taken.text


def test_admin_reset_link(admin_client: TestClient, logged_in: LoggedIn, browser: Browser) -> None:
    response = admin_client.post(f"/admin/users/{logged_in.user_id}/reset-link")
    assert response.status_code == 200
    assert use_link(browser(), link_token(response.text)) == 303


# --- Escaping -----------------------------------------------------------------------------------

PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    '<a href="javascript:alert(1)">x</a>',
    "javascript:alert(1)",
]


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str | None] = []
        self.imgs = 0
        self.handlers: list[str] = []
        self.js_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts.append(dict(attrs).get("src"))
        if tag == "img":
            self.imgs += 1
        for name, value in attrs:
            if name.startswith("on"):
                self.handlers.append(name)
            if name in ("href", "src", "action", "formaction") and (
                value or ""
            ).strip().lower().startswith("javascript:"):
                self.js_urls.append(value or "")


def assert_inert(html: str, *, shows: list[str]) -> None:
    scan = _Scan()
    scan.feed(html)
    assert scan.scripts == ["/static/htmx.min.js", "/static/app.js"]
    assert scan.imgs == 0
    assert scan.handlers == []
    assert scan.js_urls == []
    for payload in PAYLOADS[:3]:
        assert payload not in html
    for payload in shows:
        assert str(escape(payload)) in html


def test_html_in_request_fields_is_escaped_everywhere(
    admin_client: TestClient, accts: Accounts, anon: TestClient
) -> None:
    company = " ".join(PAYLOADS[:2])
    contact = " ".join(PAYLOADS[1:3])
    note = "\n".join(PAYLOADS)
    page = anon.get("/request-access")
    response = anon.post(
        "/request-access",
        data={
            "form_token": form_token(page.text),
            "company_name": company,
            "contact_name": contact,
            "email": "xss@org.test",
            "volume_band": "under_1k",
            "note": note,
            "website": "",
        },
    )
    assert response.status_code == 200
    assert accts.sql("SELECT company_name FROM access_requests")[0][0] == company

    assert_inert(admin_client.get("/admin/requests").text, shows=[company, contact, note])
    request_id = accts.sql("SELECT id FROM access_requests")[0][0]
    approved = admin_client.post(f"/admin/requests/{request_id}/approve")
    assert approved.status_code == 200
    assert_inert(approved.text, shows=[company])
    org_id = accts.sql("SELECT id FROM organizations")[0][0]
    assert_inert(admin_client.get("/admin/orgs").text, shows=[company])
    assert_inert(admin_client.get(f"/admin/orgs/{org_id}").text, shows=[company])
    assert_inert(
        admin_client.get("/admin/requests?show=handled").text, shows=[company, contact, note]
    )


def test_invalid_form_echo_is_escaped(anon: TestClient) -> None:
    page = anon.get("/request-access")
    response = anon.post(
        "/request-access",
        data={
            "form_token": form_token(page.text),
            "company_name": PAYLOADS[1],
            "contact_name": PAYLOADS[0],
            "email": "not-an-email",
            "volume_band": "under_1k",
            "note": PAYLOADS[2],
            "website": "",
        },
    )
    assert response.status_code == 400
    assert_inert(response.text, shows=PAYLOADS[:3])


def test_autoescape_is_on_for_every_template() -> None:
    names = sorted(p.name for p in TEMPLATES_DIR.iterdir() if p.is_file())
    assert names
    env = templates.env
    for name in names:
        assert name.endswith(".html"), name  # select_autoescape covers .html
        enabled = env.autoescape(name) if callable(env.autoescape) else env.autoescape
        assert enabled is True, name
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        compact = source.replace(" ", "")
        assert "|safe" not in compact, name
        assert "autoescapefalse" not in compact, name
        assert "Markup(" not in source, name
        env.get_template(name)  # compiles
    rendered = env.from_string("{{ v }}").render(v="<b>")  # strings default to escaped too
    assert rendered == "&lt;b&gt;"


# --- Logs ---------------------------------------------------------------------------------------


def test_no_emails_or_tokens_in_logs(
    caplog: pytest.LogCaptureFixture,
    admin_client: TestClient,
    accts: Accounts,
    anon: TestClient,
    logged_in: LoggedIn,
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")  # statements, never DEBUG rows
    page = anon.get("/request-access")
    anon.post(
        "/request-access",
        data={
            "form_token": form_token(page.text),
            "company_name": "Logged Co (test)",
            "contact_name": "Log Person",
            "email": "applicant.log@org.test",
            "volume_band": "under_1k",
            "note": "secret note text",
            "website": "",
        },
    )
    request_id = accts.sql("SELECT id FROM access_requests")[0][0]
    approve_token = link_token(admin_client.post(f"/admin/requests/{request_id}/approve").text)
    # The test's own queries bind emails as parameters, so only the app could leak them.
    by_email = "SELECT id, organization_id FROM users WHERE email = :e"
    _, org_id = accts.sql(by_email, e="applicant.log@org.test")[0]
    invite_token = link_token(
        admin_client.post(
            f"/admin/orgs/{org_id}/invite",
            data={"email": "invited.log@org.test", "role": "member"},
        ).text
    )
    owner = logged_in.client(follow_redirects=False)
    team_token = link_token(owner.post("/team/invite", data={"email": "team.log@org.test"}).text)
    member_id = accts.sql(by_email, e="team.log@org.test")[0][0]
    reset_token = link_token(owner.post(f"/team/users/{member_id}/reset-link").text)
    owner.post(f"/team/users/{member_id}/disable")

    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(f"{r.getMessage()} {r.args!s}" for r in records)
    assert any(r.name.startswith("sqlalchemy") for r in records), "SQL log not captured"
    secrets = [
        "applicant.log",
        "invited.log",
        "team.log",
        "Log Person",
        "secret note text",
        ADMIN_EMAIL,
        approve_token,
        invite_token,
        team_token,
        reset_token,
        NEW_PASSWORD,
    ]
    assert [s for s in secrets if s in logged] == []

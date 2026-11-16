"""The owner's team page (/team). All data is synthetic."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.management import MSG_OTHER_ACCOUNT, MSG_PENDING_OWNER
from app.db.models import Role
from app.web.auth import CSRF_HEADER, MSG_FORBIDDEN
from tests.web.conftest import Accounts, LoggedIn
from tests.web.management_routes import (
    TEAM_GETS,
    TEAM_POSTS,
    app_routes,
    audit_actions,
    count,
    fill,
    link_token,
    use_link,
)

Browser = Callable[..., TestClient]


@dataclass
class Team:
    org_id: int
    owner: LoggedIn
    member: int
    co_owner: int
    other_org_user: int
    superadmin: int

    def client(self) -> TestClient:
        return self.owner.client(follow_redirects=False)


@pytest.fixture
def team(web_app: FastAPI, accts: Accounts) -> Team:
    org_id = accts.org("Team Org (test)")
    owner = accts.user("owner@org.test", org_id=org_id, role=Role.OWNER)
    return Team(
        org_id=org_id,
        owner=LoggedIn(web_app, owner, accts.session(owner)),
        member=accts.user("member@org.test", org_id=org_id, role=Role.MEMBER),
        co_owner=accts.user("co-owner@org.test", org_id=org_id, role=Role.OWNER),
        other_org_user=accts.user("stranger@org.test", role=Role.MEMBER),  # another org
        superadmin=accts.user("admin@org.test", superadmin=True, role=Role.MEMBER),
    )


def disabled(accts: Accounts, user_id: int) -> bool:
    return accts.sql("SELECT disabled FROM users WHERE id = :i", i=user_id)[0][0]


def test_team_routes_are_the_known_ones(web_app: FastAPI) -> None:
    expected = [("GET", p) for p in TEAM_GETS] + [("POST", p) for p in TEAM_POSTS]
    assert app_routes(web_app, "/team") == sorted(expected)


def test_team_page_lists_own_organization(team: Team) -> None:
    page = team.client().get("/team")
    assert page.status_code == 200
    for email in ("owner@org.test", "member@org.test", "co-owner@org.test"):
        assert email in page.text
    assert "stranger@org.test" not in page.text
    assert "admin@org.test" not in page.text
    assert "owner@org.test (you)" in page.text
    assert f'action="/team/users/{team.member}/disable"' in page.text
    assert f"/team/users/{team.co_owner}/" not in page.text  # no buttons for owners
    assert 'href="/team">Team</a>' in page.text


def test_owner_invites_member(team: Team, accts: Accounts, browser: Browser) -> None:
    response = team.client().post(
        "/team/invite", data={"email": "New.Person@org.test", "role": "owner"}
    )
    assert response.status_code == 200
    assert 'data-copy-target="one-time-link"' in response.text
    row = accts.sql(
        "SELECT id, role, organization_id FROM users WHERE email = 'new.person@org.test'"
    )[0]
    assert (row[1], row[2]) == ("member", team.org_id)  # the submitted role was ignored
    assert count(accts, "invites", "role = 'member'") == 1
    assert audit_actions(accts) == [
        ("invite_created", "success", team.owner.user_id, row[0], team.org_id)
    ]
    assert use_link(browser(), link_token(response.text)) == 303


def test_owner_cannot_reinvite_pending_owner(team: Team, accts: Accounts) -> None:
    pending = accts.user("pending.owner@org.test", org_id=team.org_id, password_hash=None)
    response = team.client().post("/team/invite", data={"email": "pending.owner@org.test"})
    assert response.status_code == 400
    assert MSG_PENDING_OWNER in response.text
    assert accts.sql("SELECT role FROM users WHERE id = :i", i=pending)[0][0] == "owner"
    assert count(accts, "invites") == 0


def test_owner_cannot_invite_another_organizations_user(team: Team, accts: Accounts) -> None:
    response = team.client().post("/team/invite", data={"email": "stranger@org.test"})
    assert response.status_code == 400
    assert MSG_OTHER_ACCOUNT in response.text
    assert count(accts, "invites") == 0


def test_owner_invite_rejects_bad_email(team: Team, accts: Accounts) -> None:
    response = team.client().post("/team/invite", data={"email": "nope"})
    assert response.status_code == 400
    assert count(accts, "invites") == 0


def test_owner_reset_link_for_member(team: Team, accts: Accounts, browser: Browser) -> None:
    response = team.client().post(f"/team/users/{team.member}/reset-link")
    assert response.status_code == 200
    assert use_link(browser(), link_token(response.text)) == 303
    assert audit_actions(accts)[0] == (
        "password_link_created",
        "success",
        team.owner.user_id,
        team.member,
        team.org_id,
    )


def test_disabling_member_ends_sessions(web_app: FastAPI, team: Team, accts: Accounts) -> None:
    member = LoggedIn(web_app, team.member, accts.session(team.member)).client(
        follow_redirects=False
    )
    assert member.get("/").status_code == 200
    response = team.client().post(f"/team/users/{team.member}/disable")
    assert response.status_code == 303
    assert response.headers["location"] == "/team"
    assert disabled(accts, team.member)
    assert count(accts, "sessions", "user_id = :u", u=team.member) == 0
    assert member.get("/").status_code == 303

    assert team.client().post(f"/team/users/{team.member}/reset-link").status_code == 400
    assert team.client().post(f"/team/users/{team.member}/enable").status_code == 303
    assert not disabled(accts, team.member)
    assert [(a[0], a[3]) for a in audit_actions(accts)] == [
        ("user_disabled", team.member),
        ("user_enabled", team.member),
    ]


@pytest.mark.parametrize("target", ["co_owner", "self", "other_org_user", "superadmin", "unknown"])
@pytest.mark.parametrize("action", ["disable", "enable", "reset-link"])
def test_owner_gets_404_outside_scope(
    team: Team, accts: Accounts, target: str, action: str
) -> None:
    user_id = {
        "co_owner": team.co_owner,
        "self": team.owner.user_id,
        "other_org_user": team.other_org_user,
        "superadmin": team.superadmin,
        "unknown": 999_999,
    }[target]
    response = team.client().post(f"/team/users/{user_id}/{action}")
    assert response.status_code == 404
    assert count(accts, "users", "disabled") == 0
    assert count(accts, "password_tokens") == 0
    assert count(accts, "audit_log") == 0


def test_member_gets_404_on_team(web_app: FastAPI, team: Team, accts: Accounts) -> None:
    c = LoggedIn(web_app, team.member, accts.session(team.member)).client(follow_redirects=False)
    unknown = c.get("/no-such-page")
    for method, path in app_routes(web_app, "/team"):
        url = fill(path, user_id=team.co_owner)
        response = c.request(
            method, url, data={"email": "x@org.test"} if method == "POST" else None
        )
        assert response.status_code == 404, (method, path)
        assert response.content == unknown.content
    assert 'href="/team"' not in c.get("/").text
    assert count(accts, "invites") == count(accts, "audit_log") == 0


def test_superadmin_without_organization_gets_404_on_team(
    web_app: FastAPI, team: Team, accts: Accounts
) -> None:
    c = LoggedIn(web_app, team.superadmin, accts.session(team.superadmin)).client()
    assert c.get("/team").status_code == 404


@pytest.mark.parametrize("token", [None, "wrong"])
def test_csrf_required_on_every_team_post(team: Team, accts: Accounts, token: str | None) -> None:
    c = team.client()
    del c.headers[CSRF_HEADER]
    for path in TEAM_POSTS:
        data = {"email": "x@org.test"}
        if token is not None:
            data["csrf_token"] = token
        response = c.post(fill(path, user_id=team.member), data=data)
        assert response.status_code == 403, path
        assert response.text == MSG_FORBIDDEN
    assert count(accts, "audit_log") == count(accts, "invites") == 0
    assert not disabled(accts, team.member)

"""The /admin and /team routes, listed once, plus helpers for their tests. Synthetic data."""

from __future__ import annotations

import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.tokens import FORM_TOKEN_MAX_AGE_SECONDS, sign_form_token
from app.web.auth import PREAUTH_COOKIE
from tests.web.conftest import TEST_SECRET_KEY, Accounts

ADMIN_GETS = ["/admin", "/admin/requests", "/admin/orgs", "/admin/orgs/{org_id}"]
ADMIN_POSTS = [
    "/admin/requests/{request_id}/approve",
    "/admin/requests/{request_id}/reject",
    "/admin/orgs/{org_id}/disable",
    "/admin/orgs/{org_id}/enable",
    "/admin/orgs/{org_id}/quota",
    "/admin/orgs/{org_id}/invite",
    "/admin/users/{user_id}/reset-link",
    "/admin/users/{user_id}/disable",
    "/admin/users/{user_id}/enable",
]
TEAM_GETS = ["/team"]
TEAM_POSTS = [
    "/team/invite",
    "/team/users/{user_id}/reset-link",
    "/team/users/{user_id}/disable",
    "/team/users/{user_id}/enable",
]
MANAGEMENT_PATHS = {*ADMIN_GETS, *ADMIN_POSTS, *TEAM_GETS, *TEAM_POSTS}
MANAGEMENT_POSTS = [*ADMIN_POSTS, *TEAM_POSTS]

LINK_IN_PAGE = re.compile(r'value="http://testserver/invite/([A-Za-z0-9_-]{43})"')
FORM_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')
NEW_PASSWORD = "a brand new synthetic passphrase"


def fill(path: str, *, org_id: int = 1, user_id: int = 1, request_id: int = 1) -> str:
    return path.format(org_id=org_id, user_id=user_id, request_id=request_id)


def app_routes(app: FastAPI, prefix: str) -> list[tuple[str, str]]:
    """``(method, path)`` of every route of ``app`` under ``prefix``, from the app itself."""
    found = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        for r in contexts() if callable(contexts) else [route]:
            path = getattr(r, "path", "")
            if path == prefix or path.startswith(prefix + "/"):
                found += [(m, path) for m in sorted(getattr(r, "methods", None) or ())]
    return sorted(set(found) - {("HEAD", p) for _, p in found})


def link_token(html: str) -> str:
    match = LINK_IN_PAGE.search(html)
    assert match, "no one-time link on the page"
    return match.group(1)


def form_token(html: str) -> str:
    match = FORM_TOKEN.search(html)
    assert match, "no form token on the page"
    return match.group(1)


STALE_WAYS = ("token_too_old", "cookie_gone")


def stale_form_token(c: TestClient, page_html: str, purpose: str, how: str) -> str:
    """A genuine form token for ``purpose`` that the server must refuse as timed out:
    signed over an hour ago (``token_too_old``), or the page's own token after this
    browser lost its pre-auth cookie (``cookie_gone``, as when the cookie expires)."""
    if how == "token_too_old":
        return sign_form_token(
            TEST_SECRET_KEY.encode(),
            purpose,
            c.cookies[PREAUTH_COOKIE],
            now=time.time() - FORM_TOKEN_MAX_AGE_SECONDS - 1,
        )
    assert how == "cookie_gone", how
    c.cookies.delete(PREAUTH_COOKIE)
    return form_token(page_html)


def use_link(c: TestClient, token: str) -> int:
    """Set ``NEW_PASSWORD`` through the one-time link; returns the final status code."""
    page = c.get(f"/invite/{token}")
    if page.status_code != 200:
        return page.status_code
    data = {
        "form_token": form_token(page.text),
        "password": NEW_PASSWORD,
        "password_confirm": NEW_PASSWORD,
    }
    return c.post(f"/invite/{token}", data=data).status_code


def count(accts: Accounts, table: str, where: str = "true", **params: object) -> int:
    return accts.sql(f"SELECT count(*) FROM {table} WHERE {where}", **params)[0][0]  # noqa: S608


def audit_actions(accts: Accounts) -> list[tuple]:
    """``(action, outcome, user_id, target_user_id, organization_id)`` in order."""
    rows = accts.sql(
        "SELECT action, outcome, user_id, target_user_id, organization_id FROM audit_log "
        "ORDER BY id"
    )
    return [tuple(r) for r in rows]

"""Shared helpers for the /admin and /team pages.

- ``in_transaction`` runs one domain function in a database transaction (call it
  through ``run_in_threadpool``). An exception rolls the whole transaction back.
- ``not_found`` is the 404 for IDs the user may not see and for pages the user may
  not open: the same response FastAPI gives for an unknown URL, so nothing tells
  them apart.
- ``link_page`` shows a freshly made one-time link once, with a Copy button
  (``app.js``). Only the link's SHA-256 is stored, so it cannot be shown again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from app.admin import link as invite_url
from app.auth import management
from app.auth.accounts import CurrentUser, IpPrefix
from app.web.auth import NO_STORE
from app.web.templating import templates

MAX_ID = 2**63 - 1  # bigint
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

UserAction = Literal["disable", "enable", "reset-link"]


def not_found() -> HTTPException:
    return HTTPException(status_code=404)


def in_transaction[T](
    sessions: sessionmaker[Session], fn: Callable[..., T], *args: Any, **kw: Any
) -> T:
    with sessions.begin() as db:
        return fn(db, *args, **kw)


def render(
    request: Request, template: str, context: dict[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, context, status_code=status_code, headers=NO_STORE
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=NO_STORE)


def link_page(
    request: Request,
    *,
    heading: str,
    email: str,
    token: str,
    back_url: str,
    back_label: str,
) -> HTMLResponse:
    return render(
        request,
        "link_created.html",
        {
            "heading": heading,
            "email": email,
            "link": invite_url(request.app.state.settings, token),
            "back_url": back_url,
            "back_label": back_label,
        },
    )


@dataclass(frozen=True, slots=True)
class UserActionResult:
    organization_id: int | None
    email: str
    token: str | None = None  # set for "reset-link"


def user_action(
    db: Session,
    actor: CurrentUser,
    user_id: int,
    action: UserAction,
    *,
    admin: bool,
    ip: IpPrefix | None,
    now: datetime,
) -> UserActionResult | None:
    """Disable, enable or make a reset link for a user in the actor's scope.

    ``None`` if the user is out of scope (the route answers 404). Raises ``AdminError``.
    """
    user = management.scoped_user(db, actor, user_id, admin=admin)
    if user is None:
        return None
    token = None
    if action == "reset-link":
        token = management.password_link(db, actor, user, ip=ip, now=now)
    else:
        management.set_user_disabled(db, actor, user, action == "disable", ip=ip, now=now)
    return UserActionResult(user.organization_id, user.email, token)

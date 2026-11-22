"""The owner's team page (/team): list members, invite members, reset links, disable/enable.

Only owners (role owner, with an organization) may open it; members get the same
404 as for an unknown URL. Owners can invite members only (owners are created by
the superadmin), and act only on members of their own organization: owners
(themselves included) and other organizations' users give 404
(``management.scoped_user`` with ``admin=False``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

from app.auth import management
from app.auth.accounts import AdminError, CurrentUser
from app.db.models import Role
from app.web.auth import client_ip_prefix, read_small_form, require_user, sessions_of, utc_now
from app.web.pages import (
    IdParam,
    UserAction,
    in_transaction,
    link_page,
    not_found,
    redirect,
    render,
    user_action,
)

TEAM_PATH = "/team"


async def require_owner(user: Annotated[CurrentUser, Depends(require_user)]) -> CurrentUser:
    if user.role != Role.OWNER or user.organization_id is None:
        raise not_found()
    return user


Owner = Annotated[CurrentUser, Depends(require_owner)]

router = APIRouter(prefix=TEAM_PATH, dependencies=[Depends(require_user), Depends(require_owner)])


async def _team_page(
    request: Request, owner: CurrentUser, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    users = await run_in_threadpool(
        in_transaction, sessions_of(request), management.members, owner.organization_id
    )
    return render(
        request,
        "team.html",
        {"users": users, "me": owner.user_id, "error": error},
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
async def team_page(request: Request, owner: Owner) -> HTMLResponse:
    return await _team_page(request, owner)


@router.post("/invite", response_model=None)
async def invite_member(request: Request, owner: Owner) -> Response:
    form = await read_small_form(request)
    email = form.get("email", "")
    if owner.organization_id is None:  # require_owner already checked
        raise not_found()
    try:
        token = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            management.invite_user,
            owner,
            owner.organization_id,
            email,
            Role.MEMBER,  # a submitted role is ignored: owners invite members only
            admin=False,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        return await _team_page(request, owner, error=str(exc), status_code=400)
    return await link_page(
        request,
        kind="invite",
        heading="Invite link",
        email=email.strip().lower(),
        token=token,
        back_url=TEAM_PATH,
        back_label="Back to the team",
        actor_id=owner.user_id,
        organization_id=owner.organization_id,
        role=Role.MEMBER,
    )


async def _user_route(
    request: Request, owner: CurrentUser, user_id: int, action: UserAction
) -> Response:
    try:
        result = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            user_action,
            owner,
            user_id,
            action,
            admin=False,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        return await _team_page(request, owner, error=str(exc), status_code=400)
    if result is None:
        raise not_found()
    if result.token is not None:
        return await link_page(
            request,
            kind="reset",
            heading="Password reset link",
            email=result.email,
            token=result.token,
            back_url=TEAM_PATH,
            back_label="Back to the team",
            actor_id=owner.user_id,
            organization_id=result.organization_id,
        )
    return redirect(TEAM_PATH)


@router.post("/users/{user_id}/reset-link", response_model=None)
async def member_reset_link(request: Request, owner: Owner, user_id: IdParam) -> Response:
    return await _user_route(request, owner, user_id, "reset-link")


@router.post("/users/{user_id}/disable", response_model=None)
async def disable_member(request: Request, owner: Owner, user_id: IdParam) -> Response:
    return await _user_route(request, owner, user_id, "disable")


@router.post("/users/{user_id}/enable", response_model=None)
async def enable_member(request: Request, owner: Owner, user_id: IdParam) -> Response:
    return await _user_route(request, owner, user_id, "enable")

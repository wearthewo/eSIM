"""Superadmin pages: access requests, organizations and their users (/admin...).

Only superadmins may open them. Everyone else who is logged in gets the same 404
as for an unknown URL (``require_superadmin``); logged-out visitors are sent to
/login like on every other page. Unknown IDs also give 404.

Every POST needs the session's CSRF token (``require_user``) and adds an audit row.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import access, management
from app.auth.access import ApproveOutcome
from app.auth.accounts import AdminError, CurrentUser
from app.auth.management import Member
from app.db.models import Organization, Role, User
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
from app.web.public import VOLUME_LABELS

MSG_ALREADY_HANDLED = "This request has already been handled."
MSG_EXISTING_USER = (
    "{email} already has an account. Nothing was created and the request is still pending."
)
MSG_BAD_ROLE = "Choose owner or member."


async def require_superadmin(user: Annotated[CurrentUser, Depends(require_user)]) -> CurrentUser:
    if not user.is_superadmin:
        raise not_found()
    return user


Admin = Annotated[CurrentUser, Depends(require_superadmin)]

router = APIRouter(
    prefix="/admin", dependencies=[Depends(require_user), Depends(require_superadmin)]
)


@router.get("", response_model=None)
async def admin_home() -> RedirectResponse:
    return redirect("/admin/orgs")


# --- Access requests -----------------------------------------------------------------------


async def _requests_page(
    request: Request, *, handled: bool, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    rows = await run_in_threadpool(
        in_transaction, sessions_of(request), access.list_requests, handled=handled
    )
    return render(
        request,
        "admin_requests.html",
        {"rows": rows, "handled": handled, "bands": VOLUME_LABELS, "error": error},
        status_code=status_code,
    )


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, show: str = "") -> HTMLResponse:
    return await _requests_page(request, handled=show == "handled")


@router.post("/requests/{request_id}/approve", response_model=None)
async def approve_request(request: Request, admin: Admin, request_id: IdParam) -> Response:
    try:
        result = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            access.approve,
            request_id,
            admin_id=admin.user_id,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        return await _requests_page(request, handled=False, error=str(exc), status_code=400)
    if result is None:
        raise not_found()
    if result.outcome == ApproveOutcome.ALREADY_HANDLED:
        return await _requests_page(
            request, handled=False, error=MSG_ALREADY_HANDLED, status_code=409
        )
    if result.outcome == ApproveOutcome.EXISTING_USER:
        return await _requests_page(
            request,
            handled=False,
            error=MSG_EXISTING_USER.format(email=result.email),
            status_code=409,
        )
    return link_page(
        request,
        heading=f"Approved: {result.company_name}",
        email=result.email,
        token=result.token or "",
        back_url=f"/admin/orgs/{result.organization_id}",
        back_label="Go to the new organization",
    )


@router.post("/requests/{request_id}/reject", response_model=None)
async def reject_request(request: Request, admin: Admin, request_id: IdParam) -> Response:
    rejected = await run_in_threadpool(
        in_transaction,
        sessions_of(request),
        access.reject,
        request_id,
        admin_id=admin.user_id,
        ip=client_ip_prefix(request),
        now=utc_now(),
    )
    if rejected is None:
        raise not_found()
    if not rejected:
        return await _requests_page(
            request, handled=False, error=MSG_ALREADY_HANDLED, status_code=409
        )
    return redirect("/admin/requests")


# --- Organizations ---------------------------------------------------------------------------


@router.get("/orgs", response_class=HTMLResponse)
async def orgs_page(request: Request) -> HTMLResponse:
    orgs = await run_in_threadpool(
        in_transaction, sessions_of(request), management.list_orgs, today=utc_now().date()
    )
    return render(request, "admin_orgs.html", {"orgs": orgs})


def _org_detail(db: Session, org_id: int) -> tuple[Organization, list[Member]] | None:
    org = management.get_org(db, org_id)
    if org is None:
        return None
    return org, management.members(db, org_id)


async def _org_page(
    request: Request, org_id: int, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    found = await run_in_threadpool(in_transaction, sessions_of(request), _org_detail, org_id)
    if found is None:
        raise not_found()
    org, users = found
    return render(
        request,
        "admin_org.html",
        {
            "org": org,
            "users": users,
            "roles": [r.value for r in Role],
            "max_quota": management.MAX_DAILY_ROW_QUOTA,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
async def org_page(request: Request, org_id: IdParam) -> HTMLResponse:
    return await _org_page(request, org_id)


async def _set_org_disabled(
    request: Request, admin: CurrentUser, org_id: int, disabled: bool
) -> Response:
    try:
        found = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            management.set_org_disabled,
            admin,
            org_id,
            disabled,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        return await _org_page(request, org_id, error=str(exc), status_code=400)
    if not found:
        raise not_found()
    return redirect(f"/admin/orgs/{org_id}")


@router.post("/orgs/{org_id}/disable", response_model=None)
async def disable_org(request: Request, admin: Admin, org_id: IdParam) -> Response:
    return await _set_org_disabled(request, admin, org_id, True)


@router.post("/orgs/{org_id}/enable", response_model=None)
async def enable_org(request: Request, admin: Admin, org_id: IdParam) -> Response:
    return await _set_org_disabled(request, admin, org_id, False)


@router.post("/orgs/{org_id}/quota", response_model=None)
async def change_quota(request: Request, admin: Admin, org_id: IdParam) -> Response:
    form = await read_small_form(request)
    try:
        quota = management.parse_quota(form.get("daily_row_quota", ""))
    except AdminError as exc:
        return await _org_page(request, org_id, error=str(exc), status_code=400)
    found = await run_in_threadpool(
        in_transaction,
        sessions_of(request),
        management.set_quota,
        admin,
        org_id,
        quota,
        ip=client_ip_prefix(request),
        now=utc_now(),
    )
    if not found:
        raise not_found()
    return redirect(f"/admin/orgs/{org_id}")


@router.post("/orgs/{org_id}/invite", response_model=None)
async def invite_to_org(request: Request, admin: Admin, org_id: IdParam) -> Response:
    form = await read_small_form(request)
    email = form.get("email", "")
    try:
        role = Role(form.get("role", ""))
    except ValueError:
        return await _org_page(request, org_id, error=MSG_BAD_ROLE, status_code=400)
    try:
        token = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            management.invite_user,
            admin,
            org_id,
            email,
            role,
            admin=True,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        return await _org_page(request, org_id, error=str(exc), status_code=400)
    return link_page(
        request,
        heading=f"Invite link ({role.value})",
        email=email.strip().lower(),
        token=token,
        back_url=f"/admin/orgs/{org_id}",
        back_label="Back to the organization",
    )


# --- Users -------------------------------------------------------------------------------------


async def _user_route(
    request: Request, admin: CurrentUser, user_id: int, action: UserAction
) -> Response:
    try:
        result = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            user_action,
            admin,
            user_id,
            action,
            admin=True,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except AdminError as exc:
        org_id = await run_in_threadpool(in_transaction, sessions_of(request), _org_of, user_id)
        if org_id is None:
            raise not_found() from None
        return await _org_page(request, org_id, error=str(exc), status_code=400)
    if result is None:
        raise not_found()
    back = f"/admin/orgs/{result.organization_id}" if result.organization_id else "/admin/orgs"
    if result.token is not None:
        return link_page(
            request,
            heading="Password reset link",
            email=result.email,
            token=result.token,
            back_url=back,
            back_label="Back",
        )
    return redirect(back)


def _org_of(db: Session, user_id: int) -> int | None:
    user = db.get(User, user_id)
    return user.organization_id if user is not None else None


@router.post("/users/{user_id}/reset-link", response_model=None)
async def user_reset_link(request: Request, admin: Admin, user_id: IdParam) -> Response:
    return await _user_route(request, admin, user_id, "reset-link")


@router.post("/users/{user_id}/disable", response_model=None)
async def disable_user(request: Request, admin: Admin, user_id: IdParam) -> Response:
    return await _user_route(request, admin, user_id, "disable")


@router.post("/users/{user_id}/enable", response_model=None)
async def enable_user(request: Request, admin: Admin, user_id: IdParam) -> Response:
    return await _user_route(request, admin, user_id, "enable")

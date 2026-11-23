""" "Forgot password?" (``GET``/``POST /forgot-password``), only when email is on.

- With ``EMAIL_BACKEND=none`` both routes answer 404 and the login page does not link
  here (an administrator's reset link is the only way, as before).
- Same pre-auth CSRF protection as /login: a signed form token tied to the
  SameSite=Strict pre-auth cookie (``app.web.auth.form_page`` / ``check_form``).
- Every valid submission gets the same page, whatever happened (link sent, unknown
  address, disabled user, no password yet, rate limit), with no cookie set. The email
  is sent in a background task after the response, so the response time does not
  depend on whether the account exists either. Only a malformed address is shown as
  an input error.
- ``accounts.request_password_reset`` decides: enabled users with a password only,
  1-hour single-use links, at most 3 per hour per account and per IP prefix, audit
  rows without the email. Setting the new password never changes two-factor
  authentication: a user with 2FA still has to enter the code.
- Nothing about the submission (address, link) is logged.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.background import BackgroundTask

from app import emails
from app.admin import link as set_password_url
from app.auth import accounts
from app.auth.accounts import RESET_LINK_TTL, AdminError, IpPrefix, ResetGrant
from app.mail import AuditContext, deliver
from app.web.auth import (
    MSG_TIMED_OUT,
    NO_STORE,
    FormTimedOut,
    check_form,
    client_ip_prefix,
    form_page,
    read_small_form,
    sessions_of,
    typed_email,
    utc_now,
)
from app.web.templating import templates

FORGOT_PATH = "/forgot-password"
FORGOT_PURPOSE = "forgot-password"
MSG_EMAIL = "Enter a valid email address."

router = APIRouter()


def email_enabled(request: Request) -> bool:
    return request.app.state.mailer is not None


def _require_email(request: Request) -> None:
    if not email_enabled(request):
        raise HTTPException(status_code=404)


def _form(
    request: Request,
    *,
    email: str = "",
    error: str | None = None,
    status_code: int = 200,
    form_token: str | None = None,
) -> HTMLResponse:
    return form_page(
        request,
        "forgot_password.html",
        FORGOT_PURPOSE,
        {"email": email, "error": error, "max_email_chars": accounts.MAX_EMAIL_CHARS},
        status_code=status_code,
        form_token=form_token,
    )


def _grant(sessions: sessionmaker[Session], email: str, ip: IpPrefix | None) -> ResetGrant | None:
    with sessions.begin() as db:  # committed before the email is sent
        return accounts.request_password_reset(db, email, ip=ip, now=utc_now())


def _send(app: FastAPI, grant: ResetGrant, ip: IpPrefix | None) -> None:
    """Background task (after the response): email the link; a failure is audited."""
    settings = app.state.settings
    message = emails.reset(
        grant.email,
        link=set_password_url(settings, grant.token),
        hours=RESET_LINK_TTL // timedelta(hours=1),
        base_url=settings.public_base_url,
        requested=True,
    )
    audit = AuditContext(organization_id=grant.organization_id, target_user_id=grant.user_id, ip=ip)
    deliver(app.state.mailer, message, sessions=app.state.sessionmaker, audit=audit)


@router.get(FORGOT_PATH, response_class=HTMLResponse)
async def forgot_form(request: Request) -> HTMLResponse:
    _require_email(request)
    return _form(request)


@router.post(FORGOT_PATH, response_model=None)
async def forgot(request: Request) -> Response:
    _require_email(request)
    form = await read_small_form(request)
    try:
        form_token = check_form(request, FORGOT_PURPOSE, form)
    except FormTimedOut:  # refused; the same form again with a fresh token
        return _form(request, email=typed_email(form), error=MSG_TIMED_OUT, status_code=403)
    raw = form.get("email", "")
    try:
        email = accounts.normalize_email(raw)
    except AdminError:
        return _form(
            request, email=raw[:254], error=MSG_EMAIL, status_code=400, form_token=form_token
        )
    ip = client_ip_prefix(request)
    grant = await run_in_threadpool(_grant, sessions_of(request), email, ip)
    task = BackgroundTask(_send, request.app, grant, ip) if grant is not None else None
    return templates.TemplateResponse(
        request, "forgot_password_sent.html", {}, headers=NO_STORE, background=task
    )

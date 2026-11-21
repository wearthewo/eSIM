"""/account/2fa: switch two-factor authentication (TOTP) on or off (``app.auth.mfa``).

- Off: the page shows a QR code (segno, inline SVG: no image request, nothing stored,
  and no ``style=`` for the CSP) and the same key as text. The user confirms with a code
  from the app; only then is 2FA on, and the 10 recovery codes are shown, once.
- On: the status and how many recovery codes are left. "Generate new recovery codes"
  (a current code from the app, not a recovery code) replaces every old code and shows
  the new ones once. Users other than superadmins can switch 2FA off with a current code
  (or a recovery code); superadmins cannot.

Superadmins without 2FA are sent here from every other page (``app.web.auth``). Every
POST needs the session's CSRF token (``require_user``). All pages are ``no-store``. The
secret and the codes are never logged.
"""

from __future__ import annotations

from typing import Annotated, Any

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup
from sqlalchemy.orm import Session

from app.auth import mfa
from app.auth.accounts import CurrentUser
from app.web.auth import (
    ENROLMENT_PATH,
    MSG_MFA_WRONG,
    client_ip_prefix,
    mfa_keys,
    read_small_form,
    require_user,
    sessions_of,
    utc_now,
)
from app.web.pages import in_transaction, redirect, render

ENABLE_PATH = ENROLMENT_PATH + "/enable"
DISABLE_PATH = ENROLMENT_PATH + "/disable"
REGENERATE_PATH = ENROLMENT_PATH + "/recovery-codes"
CODE_FIELD = "code"

MSG_ENROL_WRONG = (
    "The code is incorrect. Check that the time on your phone is right and enter the code "
    "the app shows now."
)
MSG_SUPERADMIN_KEEPS = "Superadmins must keep two-factor authentication on."
MSG_SWITCHED_OFF = "Two-factor authentication is off."
MSG_REGENERATE_WRONG = (
    "The code is incorrect. Enter the 6-digit code your authenticator app shows now "
    "(a recovery code is not accepted here). After too many failed attempts, codes are "
    "refused for 15 minutes."
)
QR_TITLE = "QR code for your authenticator app"

router = APIRouter(dependencies=[Depends(require_user)])
User = Annotated[CurrentUser, Depends(require_user)]


def qr_svg(email: str, secret: bytes) -> Markup:
    """The otpauth URI as an inline SVG QR code (built here, never user markup)."""
    code = segno.make(mfa.otpauth_uri(email, secret), error="m")
    svg = code.svg_inline(
        scale=5, border=4, dark="#000000", light="#ffffff", svgclass="qr-code", title=QR_TITLE
    )
    return Markup(svg)  # noqa: S704  (segno output for our own URI)


def _view(db: Session, user: CurrentUser, keys: mfa.MfaKeys) -> dict[str, Any]:
    """What the page shows: the status, and while 2FA is off the pending secret's QR."""
    status = mfa.status(db, user.user_id)
    view: dict[str, Any] = {
        "status": status,
        "forced": user.is_superadmin and not status.enabled,
        "can_disable": not user.is_superadmin,
        "code_field": CODE_FIELD,
        "max_chars": mfa.MAX_CODE_CHARS,
    }
    if not status.enabled:
        secret = mfa.start_enrolment(db, user.user_id, keys)
        view["qr_svg"] = qr_svg(user.email, secret)
        view["text_key"] = mfa.text_key(secret)
    return view


def _page(request: Request, view: dict[str, Any], **extra: Any) -> HTMLResponse:
    status_code = extra.pop("status_code", 200)
    return render(request, "account_2fa.html", {**view, **extra}, status_code=status_code)


@router.get(ENROLMENT_PATH, response_class=HTMLResponse)
async def two_factor_page(request: Request, user: User) -> HTMLResponse:
    view = await run_in_threadpool(
        in_transaction, sessions_of(request), _view, user, mfa_keys(request)
    )
    notice = MSG_SWITCHED_OFF if request.query_params.get("off") == "1" else None
    return _page(request, view, notice=notice)


def _enable(
    db: Session,
    user: CurrentUser,
    code: str,
    keys: mfa.MfaKeys,
    ip: Any,
) -> list[str] | dict[str, Any]:
    """The recovery codes when the code was right; else the page to show again."""
    codes = mfa.confirm_enrolment(db, user.user_id, code, keys, now=utc_now(), ip=ip)
    if codes is not None:
        return codes
    return _view(db, user, keys)


@router.post(ENABLE_PATH, response_model=None)
async def enable(request: Request, user: User) -> Response:
    form = await read_small_form(request)
    result = await run_in_threadpool(
        in_transaction,
        sessions_of(request),
        _enable,
        user,
        form.get(CODE_FIELD, ""),
        mfa_keys(request),
        client_ip_prefix(request),
    )
    if isinstance(result, list):
        return render(request, "account_2fa_codes.html", {"codes": result})
    view = result
    if view["status"].enabled:  # switched on meanwhile (a second tab)
        return redirect(ENROLMENT_PATH)
    return _page(request, view, error=MSG_ENROL_WRONG, status_code=400)


def _disable(
    db: Session, user: CurrentUser, code: str, keys: mfa.MfaKeys, ip: Any
) -> tuple[mfa.Verdict, dict[str, Any]]:
    verdict = mfa.disable(db, user.user_id, code, keys, now=utc_now(), ip=ip)
    if verdict in (mfa.Verdict.OK, mfa.Verdict.RECOVERY):
        return verdict, {}
    return verdict, _view(db, user, keys)


@router.post(DISABLE_PATH, response_model=None)
async def disable(request: Request, user: User) -> Response:
    form = await read_small_form(request)
    verdict, view = await run_in_threadpool(
        in_transaction,
        sessions_of(request),
        _disable,
        user,
        form.get(CODE_FIELD, ""),
        mfa_keys(request),
        client_ip_prefix(request),
    )
    if verdict in (mfa.Verdict.OK, mfa.Verdict.RECOVERY):
        return redirect(ENROLMENT_PATH + "?off=1")
    if verdict == mfa.Verdict.FORBIDDEN:
        return _page(request, view, error=MSG_SUPERADMIN_KEEPS, status_code=403)
    if verdict == mfa.Verdict.UNAVAILABLE:  # already off
        return redirect(ENROLMENT_PATH)
    return _page(request, view, error=MSG_MFA_WRONG, status_code=400)


def _regenerate(
    db: Session, user: CurrentUser, code: str, keys: mfa.MfaKeys, ip: Any
) -> tuple[mfa.Verdict, list[str] | None, dict[str, Any]]:
    verdict, codes = mfa.regenerate_recovery_codes(
        db, user.user_id, code, keys, now=utc_now(), ip=ip
    )
    return verdict, codes, ({} if codes is not None else _view(db, user, keys))


@router.post(REGENERATE_PATH, response_model=None)
async def regenerate(request: Request, user: User) -> Response:
    """New recovery codes after a current TOTP code; the old ones stop working."""
    form = await read_small_form(request)
    verdict, codes, view = await run_in_threadpool(
        in_transaction,
        sessions_of(request),
        _regenerate,
        user,
        form.get(CODE_FIELD, ""),
        mfa_keys(request),
        client_ip_prefix(request),
    )
    if codes is not None:
        return render(request, "account_2fa_codes.html", {"codes": codes, "regenerated": True})
    if verdict == mfa.Verdict.UNAVAILABLE:  # 2FA is off: nothing to regenerate
        return redirect(ENROLMENT_PATH)
    return _page(request, view, error=MSG_REGENERATE_WRONG, status_code=400)

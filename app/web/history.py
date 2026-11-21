"""The History page (/history): the organization's batches, newest first, with a From/To filter.

Members and owners see their own organization's batches; a superadmin sees every
organization's, with an optional organization filter. Batches that were only checked
(status ``validated``) are not listed. Times are shown and typed in Greek time
(``app.localtime``); the database stores UTC.

The From/To fields are plain ``dd/mm/yyyy`` and ``HH:mm`` text inputs (app.js adds a
calendar popup). Input is checked here; a bad value gives a 400 with a clear message
and the form as the user typed it. Generated files are never stored, so the page has
nothing to download.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app import batches
from app.auth.accounts import CurrentUser
from app.db.models import BatchStatus, Organization, Role
from app.localtime import DateInputError, default_fields, parse_range
from app.web.auth import client_ip_prefix, read_small_form, require_user, sessions_of, utc_now
from app.web.pages import MAX_ID, IdParam, in_transaction, not_found, redirect, render

HISTORY_PATH = "/history"
VOID_PATH = HISTORY_PATH + "/batches/{batch_id}/void"
CONFIRM_FIELD = "confirm_not_sent"
VOID_WARNING = (
    "Only void this batch if none of its eSIMs were sent to customers. "
    "Its codes will be accepted again in new uploads."
)
CONFIRM_LABEL = "I confirm none were sent"
MSG_CONFIRM_REQUIRED = f"Tick “{CONFIRM_LABEL}” to void this batch."
MSG_ALREADY_VOIDED = "This batch is already voided."
MAX_FIELD_CHARS = 32  # longer than any valid date, time, id or page number
FILTER_FIELDS = ("from_date", "from_time", "to_date", "to_time")
_DIGITS = re.compile(r"[0-9]{1,19}")  # ASCII digits only

MSG_BAD_ORG = "Choose an organization from the list."
MSG_TOO_LONG = "A filter field is too long."
NOTE = "Generated files are not stored. Keep your downloaded ZIP files."

router = APIRouter(dependencies=[Depends(require_user)])


def _can_void(user: CurrentUser) -> bool:
    """Owners (their own organization's batches) and superadmins (any batch)."""
    return user.is_superadmin or (user.role == Role.OWNER and user.organization_id is not None)


def _page_number(raw: str | None) -> int:
    """A page number; anything that is not one gives page 1."""
    if raw is None or not _DIGITS.fullmatch(raw) or len(raw) > 6:
        return 1
    return max(1, int(raw))


def _organization_filter(raw: str | None) -> int | None:
    """The superadmin's organization filter: ``None`` for all. Raises ``DateInputError``."""
    if raw is None or raw == "":
        return None
    if not _DIGITS.fullmatch(raw) or not 1 <= int(raw) <= MAX_ID:
        raise DateInputError(MSG_BAD_ORG)
    return int(raw)


@router.get(HISTORY_PATH, response_class=HTMLResponse)
async def history_page(
    request: Request, user: Annotated[CurrentUser, Depends(require_user)]
) -> HTMLResponse:
    params = request.query_params
    raw = {name: params.get(name) for name in (*FILTER_FIELDS, "org", "page")}
    now = utc_now()
    sessions = sessions_of(request)
    context: dict[str, Any] = {
        "note": NOTE,
        "is_admin": user.is_superadmin,
        "can_void": _can_void(user),
        "error": None,
        "result": None,
        "org": raw["org"] or "",
        "orgs": [],
    }
    if user.is_superadmin:
        context["orgs"] = await run_in_threadpool(in_transaction, sessions, batches.organizations)

    # The form shows what was typed (or the defaults), until the input is read.
    shown = default_fields(now)
    shown.update({name: raw[name] or "" for name in FILTER_FIELDS if raw[name] is not None})
    context["fields"] = shown
    try:
        if any(value is not None and len(value) > MAX_FIELD_CHARS for value in raw.values()):
            raise DateInputError(MSG_TOO_LONG)
        organization_id = (
            _organization_filter(raw["org"]) if user.is_superadmin else user.organization_id
        )
        window = parse_range(
            raw["from_date"], raw["from_time"], raw["to_date"], raw["to_time"], now=now
        )
    except DateInputError as exc:
        context["error"] = str(exc)
        return render(request, "history.html", context, status_code=400)

    if organization_id is None and not user.is_superadmin:  # not expected: no organization
        result = batches.HistoryPage(rows=[], total=0, page=1, pages=1)
    else:
        result = await run_in_threadpool(
            in_transaction,
            sessions,
            batches.history,
            organization_id=organization_id,
            start=window.start,
            end=window.end,
            page=_page_number(raw["page"]),
        )
    context["fields"] = {
        "from_date": window.from_date,
        "from_time": window.from_time,
        "to_date": window.to_date,
        "to_time": window.to_time,
    }
    context["org"] = str(organization_id) if user.is_superadmin and organization_id else ""
    context["result"] = result
    return render(request, "history.html", context)


# --- Voiding a batch ----------------------------------------------------------------------------
# Owners (their own organization) and superadmins (any). Everyone else, other
# organizations' batches, unknown IDs and batches only checked get the same 404.


def _void_view(db: Session, actor: CurrentUser, batch_id: int) -> dict[str, Any] | None:
    batch = batches.voidable_batch(db, actor, batch_id)
    if batch is None:
        return None
    org = db.get(Organization, batch.organization_id)
    return {
        "id": batch.id,
        "file_name": batch.file_name,
        "created_at": batch.created_at,
        "row_count": batch.row_count,
        "parts": batch.parts,
        "parts_downloaded": batch.parts_downloaded,
        "organization_name": org.name if org is not None else "",
        "voided": batch.status == BatchStatus.VOIDED,
    }


def _void_page(
    request: Request,
    batch: dict[str, Any],
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "batch_void.html",
        {
            "batch": batch,
            "warning": VOID_WARNING,
            "confirm_field": CONFIRM_FIELD,
            "confirm_label": CONFIRM_LABEL,
            "error": error,
            "is_admin": request.state.user.is_superadmin,
        },
        status_code=status_code,
    )


async def _scoped_view(request: Request, user: CurrentUser, batch_id: int) -> dict[str, Any]:
    view = await run_in_threadpool(in_transaction, sessions_of(request), _void_view, user, batch_id)
    if view is None:
        raise not_found()
    return view


@router.get(VOID_PATH, response_class=HTMLResponse)
async def void_form(
    request: Request, user: Annotated[CurrentUser, Depends(require_user)], batch_id: IdParam
) -> HTMLResponse:
    """The confirmation page (a voided batch shows that it is voided, with no form)."""
    view = await _scoped_view(request, user, batch_id)
    if view["voided"]:
        return _void_page(request, view, error=MSG_ALREADY_VOIDED, status_code=409)
    return _void_page(request, view)


@router.post(VOID_PATH, response_model=None)
async def void_batch(
    request: Request, user: Annotated[CurrentUser, Depends(require_user)], batch_id: IdParam
) -> Response:
    """Void the batch (CSRF checked by ``require_user``); the checkbox is required."""
    form = await read_small_form(request)
    view = await _scoped_view(request, user, batch_id)
    if view["voided"]:
        return _void_page(request, view, error=MSG_ALREADY_VOIDED, status_code=409)
    if form.get(CONFIRM_FIELD) != "yes":
        return _void_page(request, view, error=MSG_CONFIRM_REQUIRED, status_code=400)
    try:
        done = await run_in_threadpool(
            in_transaction,
            sessions_of(request),
            batches.void,
            user,
            batch_id,
            ip=client_ip_prefix(request),
            now=utc_now(),
        )
    except batches.AlreadyVoided:  # voided by someone else meanwhile
        _forget_rows(request, batch_id)
        view["voided"] = True
        return _void_page(request, view, error=MSG_ALREADY_VOIDED, status_code=409)
    if not done:
        raise not_found()
    _forget_rows(request, batch_id)  # after the commit: a void cannot be undone
    return redirect(HISTORY_PATH)


def _forget_rows(request: Request, batch_id: int) -> None:
    """Drop the voided batch's checked rows from the upload cache (``app.upload_cache``)."""
    cache = getattr(request.app.state, "upload_cache", None)
    if cache is not None:
        cache.discard(batch_id)

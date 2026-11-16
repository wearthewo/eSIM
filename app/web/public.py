"""The public "Request access" form (``GET``/``POST /request-access``).

- Same pre-auth CSRF protection as /login: a signed form token tied to the
  SameSite=Strict pre-auth cookie (``app.web.auth.form_page`` / ``check_form``).
- Honeypot: a field named ``website``, hidden with CSS (``.hp-field``). People never
  fill it; if it is filled the request is dropped.
- Whatever happens after the form is valid (stored, honeypot, rate limit, duplicate,
  email already registered) the answer is the same thank-you page, so the form never
  reveals who has an account. Only plain input mistakes are shown to the submitter.
- Nothing about the submission is logged.
"""

from __future__ import annotations

import unicodedata

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from app.auth import access, accounts
from app.auth.access import RequestFields
from app.auth.accounts import AdminError, IpPrefix
from app.db.models import MAX_EMAIL_CHARS, MAX_NAME_CHARS, MAX_NOTE_CHARS, VolumeBand
from app.web.auth import (
    NO_STORE,
    check_form,
    client_ip_prefix,
    form_page,
    read_small_form,
    sessions_of,
    utc_now,
)
from app.web.templating import templates

REQUEST_ACCESS_PATH = "/request-access"
REQUEST_ACCESS_PURPOSE = "request-access"
HONEYPOT_FIELD = "website"
# A 2000-character note of 3-byte characters, percent-encoded, is about 18 KB.
MAX_REQUEST_FORM_BYTES = 32 * 1024

VOLUME_LABELS: dict[VolumeBand, str] = {
    VolumeBand.UNDER_1K: "Under 1,000",
    VolumeBand.FROM_1K_TO_10K: "1,000 to 10,000",
    VolumeBand.FROM_10K_TO_100K: "10,000 to 100,000",
    VolumeBand.OVER_100K: "Over 100,000",
}

MSG_COMPANY = f"Enter your company name (at most {MAX_NAME_CHARS} characters)."
MSG_NAME = f"Enter your name (at most {MAX_NAME_CHARS} characters)."
MSG_EMAIL = "Enter a valid work email address."
MSG_VOLUME = "Choose your expected monthly volume."
MSG_NOTE = f"The note can be at most {MAX_NOTE_CHARS} characters."
MSG_CONTROL = "Remove control characters from the form."

FIELDS = ("company_name", "contact_name", "email", "volume_band", "note")

router = APIRouter()


class FieldError(Exception):
    """A plain input mistake; the message is shown above the form."""


def _has_controls(text: str, allowed: str = "") -> bool:
    return any(unicodedata.category(ch) == "Cc" and ch not in allowed for ch in text)


def _line(raw: str, message: str) -> str:
    value = raw.strip()
    if not value or len(value) > MAX_NAME_CHARS:
        raise FieldError(message)
    if _has_controls(value):
        raise FieldError(MSG_CONTROL)
    return value


def _note(raw: str) -> str | None:
    value = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > MAX_NOTE_CHARS:
        raise FieldError(MSG_NOTE)
    if _has_controls(value, allowed="\n\t"):
        raise FieldError(MSG_CONTROL)
    return value or None


def parse_fields(form: dict[str, str]) -> RequestFields:
    """Validate the submission. Raises ``FieldError``."""
    company = _line(form.get("company_name", ""), MSG_COMPANY)
    contact = _line(form.get("contact_name", ""), MSG_NAME)
    try:
        email = accounts.normalize_email(form.get("email", ""))
    except AdminError:
        raise FieldError(MSG_EMAIL) from None
    try:
        band = VolumeBand(form.get("volume_band", ""))
    except ValueError:
        raise FieldError(MSG_VOLUME) from None
    return RequestFields(company, contact, email, band, _note(form.get("note", "")))


def _form(
    request: Request,
    values: dict[str, str],
    *,
    error: str | None = None,
    status_code: int = 200,
    form_token: str | None = None,
) -> HTMLResponse:
    return form_page(
        request,
        "request_access.html",
        REQUEST_ACCESS_PURPOSE,
        {
            "values": values,
            "error": error,
            "bands": VOLUME_LABELS,
            "max_name_chars": MAX_NAME_CHARS,
            "max_email_chars": MAX_EMAIL_CHARS,
            "max_note_chars": MAX_NOTE_CHARS,
        },
        status_code=status_code,
        form_token=form_token,
    )


def _thank_you(request: Request) -> HTMLResponse:
    """The one answer to every valid submission (identical bytes whatever happened)."""
    return templates.TemplateResponse(request, "request_access_done.html", {}, headers=NO_STORE)


def _submit(sessions: sessionmaker[Session], fields: RequestFields, ip: IpPrefix | None) -> None:
    with sessions.begin() as db:
        access.submit_request(db, fields, ip=ip, now=utc_now())


@router.get(REQUEST_ACCESS_PATH, response_class=HTMLResponse)
async def request_access_form(request: Request) -> HTMLResponse:
    return _form(request, {})


@router.post(REQUEST_ACCESS_PATH, response_model=None)
async def request_access(request: Request) -> Response:
    form = await read_small_form(request, max_bytes=MAX_REQUEST_FORM_BYTES)
    form_token = check_form(request, REQUEST_ACCESS_PURPOSE, form)
    if form.get(HONEYPOT_FIELD, ""):
        return _thank_you(request)
    try:
        fields = parse_fields(form)
    except FieldError as exc:
        values = {name: form.get(name, "") for name in FIELDS}
        return _form(request, values, error=str(exc), status_code=400, form_token=form_token)
    await run_in_threadpool(_submit, sessions_of(request), fields, client_ip_prefix(request))
    return _thank_you(request)

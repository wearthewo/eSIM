"""The public Privacy Policy (``GET /privacy``) and Terms of Use (``GET /terms``).

The pages state facts about this code, so every period, limit and cookie on them comes
from the constant or setting that enforces it, never from text typed into the template:

- retention: ``app.auth.retention``, ``app.auth.accounts``, ``app.batches``;
- the upload cache: ``UPLOAD_CACHE_TTL_MINUTES`` (``Settings.upload_cache_ttl_minutes``);
- cookies: the names from ``app.web.auth`` (``__Host-`` in production) and the lifetimes
  from ``app.auth.accounts`` / ``app.auth.tokens``;
- IP prefixes: ``app.db.ip``; limits: ``MAX_ROWS``, ``MAX_UPLOAD_MB``,
  ``DEFAULT_DAILY_ROW_QUOTA``.

Each period is rendered in a ``<span data-period="<key>">``, and tests/web/test_legal.py
checks every one against the code. ``OPERATOR_NAME`` and ``CONTACT_EMAIL`` are required
in production; development shows placeholders. No database access, no login.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth.accounts import LINK_TTL, RESET_LINK_TTL, SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL
from app.auth.retention import ACCESS_REQUEST_TTL, AUDIT_LOG_TTL, SPENT_LINK_TTL
from app.auth.tokens import FORM_TOKEN_MAX_AGE_SECONDS, MFA_STATE_MAX_AGE_SECONDS
from app.batches import DUPLICATE_WINDOW, STALE_AFTER
from app.db.ip import IPV4_PREFIX_LEN, IPV6_PREFIX_LEN
from app.db.models import DEFAULT_DAILY_ROW_QUOTA
from app.emails import SITE_NAME
from app.settings import Settings
from app.web.auth import MFA_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE, cookie_name
from app.web.templating import templates

LAST_UPDATED = dt.date(2026, 11, 22)
PRIVACY_PATH = "/privacy"
TERMS_PATH = "/terms"

_DAY = 24 * 60 * 60
_YEAR_DAYS = 365

router = APIRouter()


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def duration(value: dt.timedelta) -> str:
    """A period in plain words: "5 minutes", "1 hour", "48 hours", "7 days", "12 months".

    Under 3 days in hours (or minutes when not whole hours), whole years in months,
    otherwise in days. tests/web/test_legal.py reads these back."""
    seconds = int(value.total_seconds())
    if seconds % 3600:
        if seconds % 60:
            raise ValueError("periods are whole minutes")
        return _plural(seconds // 60, "minute")
    if seconds < 3 * _DAY or seconds % _DAY:
        return _plural(seconds // 3600, "hour")
    days = seconds // _DAY
    if days % _YEAR_DAYS == 0:
        return _plural(days // _YEAR_DAYS * 12, "month")
    return _plural(days, "day")


def periods(settings: Settings) -> dict[str, str]:
    """Every period the pages show, by its ``data-period`` key."""
    values = {
        "access_requests": ACCESS_REQUEST_TTL,
        "audit_log": AUDIT_LOG_TTL,
        "session_idle": SESSION_IDLE_TTL,
        "session_absolute": SESSION_ABSOLUTE_TTL,
        "invite_link": LINK_TTL,
        "reset_link": RESET_LINK_TTL,
        "spent_links": SPENT_LINK_TTL,
        "validated_batches": STALE_AFTER,
        "fingerprints": DUPLICATE_WINDOW,
        "upload_cache": dt.timedelta(minutes=settings.upload_cache_ttl_minutes),
        "preauth_cookie": dt.timedelta(seconds=FORM_TOKEN_MAX_AGE_SECONDS),
        "mfa_cookie": dt.timedelta(seconds=MFA_STATE_MAX_AGE_SECONDS),
    }
    return {key: duration(value) for key, value in values.items()}


@dataclass(frozen=True, slots=True)
class CookieRow:
    key: str  # data-period key of its lifetime
    name: str
    purpose: str


def cookie_rows(settings: Settings) -> list[CookieRow]:
    """The cookies the app sets, with their names as this server sets them."""
    return [
        CookieRow(
            "session_absolute",
            cookie_name(settings, SESSION_COOKIE),
            "Keeps you signed in.",
        ),
        CookieRow(
            "preauth_cookie",
            cookie_name(settings, PREAUTH_COOKIE),
            "Protects the sign-in, set-password, request-access and forgot-password forms "
            "against forged submissions from other websites.",
        ),
        CookieRow(
            "mfa_cookie",
            cookie_name(settings, MFA_COOKIE),
            "Remembers, for the two-factor step only, that you entered the right password.",
        ),
    ]


def _context(settings: Settings) -> dict[str, object]:
    return {
        "site_name": SITE_NAME,
        "operator": settings.operator_name,
        "contact": settings.contact_email,
        "last_updated": LAST_UPDATED,
        "period": periods(settings),
        "cookies": cookie_rows(settings),
        "ipv4_prefix": IPV4_PREFIX_LEN,
        "ipv6_prefix": IPV6_PREFIX_LEN,
        "daily_quota": DEFAULT_DAILY_ROW_QUOTA,
        "max_rows": settings.max_rows,
        "max_upload_mb": settings.max_upload_mb,
    }


def _page(request: Request, template: str) -> HTMLResponse:
    return templates.TemplateResponse(request, template, _context(request.app.state.settings))


@router.get(PRIVACY_PATH, response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return _page(request, "privacy.html")


@router.get(TERMS_PATH, response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return _page(request, "terms.html")

"""Login, logout, one-time links, and the ``require_user`` dependency.

Cookies (see ``cookie_name``):

- session: 32 random bytes; the database keeps only its SHA-256. HttpOnly,
  SameSite=Lax, Path=/. In production it is ``__Host-esim_session`` and Secure.
- pre-auth: a random nonce for the login and set-password forms. SameSite=Strict,
  so another site's form post never carries it (login CSRF).

Development runs on plain ``http://127.0.0.1``, where browsers do not reliably
keep ``Secure`` cookies (and a ``__Host-`` cookie must be Secure). So with
``ENV=development`` the cookies are named ``esim_session`` / ``esim_preauth`` and
are not Secure; everything else is the same.

CSRF: every POST behind ``require_user`` needs the session's CSRF token, in the
``X-CSRF-Token`` header (htmx, app.js) or, for urlencoded forms, the
``csrf_token`` field. /login and /invite/<token> have no session yet and use a
signed form token tied to the pre-auth cookie instead (``app.auth.tokens``).

Never log passwords, tokens or emails. ``RedactInviteTokens`` keeps link tokens
out of the uvicorn access log.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from app.auth import accounts
from app.auth.accounts import SESSION_ABSOLUTE_TTL, CurrentUser, LinkTarget
from app.auth.passwords import (
    MAX_PASSWORD_CHARS,
    MIN_PASSWORD_CHARS,
    hash_password,
    password_problem,
)
from app.auth.tokens import (
    FORM_TOKEN_MAX_AGE_SECONDS,
    check_form_token,
    csrf_for,
    csrf_matches,
    digest,
    is_token_shaped,
    new_token,
    sign_form_token,
)
from app.db.ip import ip_prefix
from app.db.models import AuditOutcome
from app.settings import Settings
from app.web.templating import templates

SESSION_COOKIE = "esim_session"
PREAUTH_COOKIE = "esim_preauth"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
FORM_TOKEN_FIELD = "form_token"  # noqa: S105  (a field name, not a secret)
LOGIN_PATH = "/login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_FORM_BYTES = 8 * 1024
MAX_FORM_FIELDS = 10

MSG_LOGIN_FAILED = (
    "Email or password is incorrect. After too many failed attempts, sign-in pauses for 15 minutes."
)
MSG_LINK_INVALID = (
    "This link is not valid. It may have been used already or have expired. "
    "Ask your administrator for a new link."
)
MSG_PASSWORDS_DIFFER = "The two passwords do not match."
MSG_FORBIDDEN = "This form has expired. Go back, reload the page and try again."
MSG_BAD_FORM = "The form could not be read. Reload the page and try again."

NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter()


# --- Errors ------------------------------------------------------------------------


class AuthError(Exception):
    """An auth failure answered with a short plain-text message."""

    status_code = 400
    message = MSG_BAD_FORM


class LoginRequired(AuthError):
    status_code = 401


class CsrfFailed(AuthError):
    status_code = 403
    message = MSG_FORBIDDEN


class FormTooLarge(AuthError):
    status_code = 413


async def handle_auth_error(request: Request, exc: Exception) -> Response:
    """Not logged in: 303 to /login (htmx: 401 + HX-Redirect). Others: plain text."""
    if isinstance(exc, LoginRequired):
        if request.headers.get("HX-Request") == "true":
            response: Response = Response(
                status_code=401, headers={"HX-Redirect": LOGIN_PATH, **NO_STORE}
            )
        else:
            response = RedirectResponse(LOGIN_PATH, status_code=303, headers=NO_STORE)
        settings = _settings(request)
        if request.cookies.get(cookie_name(settings, SESSION_COOKIE)):
            _clear_cookie(response, settings, SESSION_COOKIE)
        return response
    status_code = getattr(exc, "status_code", AuthError.status_code)
    message = getattr(exc, "message", AuthError.message)
    return PlainTextResponse(message, status_code=status_code, headers=NO_STORE)


# --- Request helpers ----------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _sessions(request: Request) -> sessionmaker[Session]:
    return request.app.state.sessionmaker


def _secret_key(request: Request) -> bytes:
    key = _settings(request).secret_key
    if key is None:  # create_app refuses to start without it
        raise RuntimeError("SECRET_KEY is not set")
    return key.reveal().encode("utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


def client_ip_prefix(request: Request) -> accounts.IpPrefix | None:
    """The client's /24 or /48, or ``None`` if the peer is not an IP address.

    Behind a proxy this is only right when uvicorn runs with ``--proxy-headers``.
    """
    if request.client is None:
        return None
    try:
        return ip_prefix(request.client.host)
    except ValueError:
        return None


async def read_small_form(request: Request) -> dict[str, str]:
    """Read an urlencoded body of at most ``MAX_FORM_BYTES`` into ``{name: first value}``.

    Streams with a cap (no unbounded read). Raises ``FormTooLarge`` or ``AuthError``.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise AuthError
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        raise FormTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise FormTooLarge
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=MAX_FORM_FIELDS,
        )
    except (UnicodeDecodeError, ValueError):
        raise AuthError from None
    return {name: values[0] for name, values in parsed.items()}


# --- Cookies ------------------------------------------------------------------------


def cookie_name(settings: Settings, base: str) -> str:
    return f"__Host-{base}" if settings.cookie_secure else base


def _set_cookie(
    response: Response, settings: Settings, base: str, value: str, *, max_age: int, samesite: str
) -> None:
    response.set_cookie(
        cookie_name(settings, base),
        value,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=samesite,  # type: ignore[arg-type]
    )


def _clear_cookie(response: Response, settings: Settings, base: str) -> None:
    response.delete_cookie(
        cookie_name(settings, base),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    _set_cookie(
        response,
        settings,
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_ABSOLUTE_TTL.total_seconds()),
        samesite="lax",
    )


def _preauth_nonce(request: Request) -> str | None:
    value = request.cookies.get(cookie_name(_settings(request), PREAUTH_COOKIE))
    return value if value and is_token_shaped(value) else None


# --- The dependency -------------------------------------------------------------------


def _resolve(sessions: sessionmaker[Session], token: str) -> CurrentUser | None:
    with sessions.begin() as db:
        return accounts.resolve_session(db, token, now=_now())


async def require_user(request: Request) -> CurrentUser:
    """The logged-in user; raises ``LoginRequired`` or (POST without CSRF token) ``CsrfFailed``.

    Sets ``request.state.user`` and ``request.state.csrf_token`` for the templates.
    """
    settings = _settings(request)
    token = request.cookies.get(cookie_name(settings, SESSION_COOKIE))
    if not token:
        raise LoginRequired
    user = await run_in_threadpool(_resolve, _sessions(request), token)
    if user is None:
        raise LoginRequired
    if request.method not in SAFE_METHODS:
        submitted = request.headers.get(CSRF_HEADER)
        if submitted is None and request.headers.get("content-type", "").lower().startswith(
            "application/x-www-form-urlencoded"
        ):
            submitted = (await read_small_form(request)).get(CSRF_FIELD)
        if not csrf_matches(submitted, user.csrf_token_hash):
            raise CsrfFailed
    request.state.user = user
    request.state.csrf_token = csrf_for(token)
    return user


# --- Pages without a session: signed form tokens --------------------------------------


def _form_page(
    request: Request,
    template: str,
    purpose: str,
    context: dict[str, object],
    *,
    status_code: int = 200,
    form_token: str | None = None,
) -> HTMLResponse:
    """Render a pre-auth form with a signed form token and (re)set the pre-auth cookie.

    Pass ``form_token`` to reuse an already checked token, so that re-rendering the
    form after a failure gives the same page whatever the cause of the failure.
    """
    settings = _settings(request)
    nonce = _preauth_nonce(request) or new_token()
    if form_token is None:
        form_token = sign_form_token(_secret_key(request), purpose, nonce)
    response = templates.TemplateResponse(
        request,
        template,
        {"form_token": form_token, **context},
        status_code=status_code,
        headers=NO_STORE,
    )
    _set_cookie(
        response,
        settings,
        PREAUTH_COOKIE,
        nonce,
        max_age=FORM_TOKEN_MAX_AGE_SECONDS,
        samesite="strict",
    )
    return response


def _check_form(request: Request, purpose: str, form: dict[str, str]) -> str:
    """The submitted form token if valid for ``purpose`` and this browser; else ``CsrfFailed``."""
    token = form.get(FORM_TOKEN_FIELD)
    if token is None or not check_form_token(
        _secret_key(request), purpose, token, _preauth_nonce(request)
    ):
        raise CsrfFailed
    return token


def _logged_in_redirect(request: Request, session_token: str) -> RedirectResponse:
    settings = _settings(request)
    response = RedirectResponse("/", status_code=303, headers=NO_STORE)
    _set_session_cookie(response, settings, session_token)
    _clear_cookie(response, settings, PREAUTH_COOKIE)
    return response


# --- /login, /logout ------------------------------------------------------------------

LOGIN_PURPOSE = "login"


def _login_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    form_token: str | None = None,
) -> HTMLResponse:
    return _form_page(
        request,
        "login.html",
        LOGIN_PURPOSE,
        {"error": error},
        status_code=status_code,
        form_token=form_token,
    )


@router.get(LOGIN_PATH, response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return _login_page(request)


def _login(
    sessions: sessionmaker[Session],
    email: str,
    password: str,
    old_token: str | None,
    ip: accounts.IpPrefix | None,
) -> str | None:
    with sessions.begin() as db:
        now = _now()
        user = accounts.attempt_login(db, email, password, ip=ip, now=now)
        if user is None:
            return None
        accounts.delete_session(db, old_token)  # a new session on every login
        return accounts.create_session(db, user.id, now=now)


@router.post(LOGIN_PATH, response_model=None)
async def login(request: Request) -> Response:
    form = await read_small_form(request)
    form_token = _check_form(request, LOGIN_PURPOSE, form)
    old_token = request.cookies.get(cookie_name(_settings(request), SESSION_COOKIE))
    session_token = await run_in_threadpool(
        _login,
        _sessions(request),
        form.get("email", ""),
        form.get("password", ""),
        old_token,
        client_ip_prefix(request),
    )
    if session_token is None:
        return _login_page(request, error=MSG_LOGIN_FAILED, status_code=401, form_token=form_token)
    return _logged_in_redirect(request, session_token)


def _logout(
    sessions: sessionmaker[Session],
    token: str | None,
    user: CurrentUser,
    ip: accounts.IpPrefix | None,
) -> None:
    with sessions.begin() as db:
        accounts.delete_session(db, token)
        accounts.audit(
            db,
            accounts.LOGOUT,
            AuditOutcome.SUCCESS,
            now=_now(),
            user_id=user.user_id,
            organization_id=user.organization_id,
            ip=ip,
        )


@router.post("/logout", response_model=None)
async def logout(request: Request, user: Annotated[CurrentUser, Depends(require_user)]) -> Response:
    settings = _settings(request)
    token = request.cookies.get(cookie_name(settings, SESSION_COOKIE))
    await run_in_threadpool(_logout, _sessions(request), token, user, client_ip_prefix(request))
    response = RedirectResponse(LOGIN_PATH, status_code=303, headers=NO_STORE)
    _clear_cookie(response, settings, SESSION_COOKIE)
    return response


# --- /invite/<token>: set a password from a one-time link -----------------------------


def _link_purpose(token: str) -> str:
    return "invite:" + digest(token).hex()


def _invalid_link(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "link_invalid.html",
        {"message": MSG_LINK_INVALID},
        status_code=404,
        headers=NO_STORE,
    )


def _set_password_page(
    request: Request,
    token: str,
    target: LinkTarget,
    *,
    error: str | None = None,
    status_code: int = 200,
    form_token: str | None = None,
) -> HTMLResponse:
    return _form_page(
        request,
        "set_password.html",
        _link_purpose(token),
        {
            "token": token,
            "email": target.email,
            "is_invite": target.is_invite,
            "error": error,
            "min_chars": MIN_PASSWORD_CHARS,
            "max_chars": MAX_PASSWORD_CHARS,
        },
        status_code=status_code,
        form_token=form_token,
    )


def _find_link(sessions: sessionmaker[Session], token: str) -> LinkTarget | None:
    with sessions.begin() as db:
        return accounts.find_link(db, token, now=_now())


@router.get("/invite/{token}", response_class=HTMLResponse)
async def link_form(request: Request, token: str) -> HTMLResponse:
    target = await run_in_threadpool(_find_link, _sessions(request), token)
    if target is None:
        return _invalid_link(request)
    return _set_password_page(request, token, target)


def _use_link(
    sessions: sessionmaker[Session],
    token: str,
    password_hash: str,
    old_token: str | None,
    ip: accounts.IpPrefix | None,
) -> str | None:
    with sessions.begin() as db:
        now = _now()
        target = accounts.consume_link(db, token, password_hash, ip=ip, now=now)
        if target is None:
            return None
        accounts.delete_session(db, old_token)
        return accounts.create_session(db, target.user_id, now=now)


@router.post("/invite/{token}", response_model=None)
async def set_password(request: Request, token: str) -> Response:
    form = await read_small_form(request)
    form_token = _check_form(request, _link_purpose(token), form)
    target = await run_in_threadpool(_find_link, _sessions(request), token)
    if target is None:
        return _invalid_link(request)

    password = form.get("password", "")
    error = (
        MSG_PASSWORDS_DIFFER
        if password != form.get("password_confirm", "")
        else password_problem(password, target.email)
    )
    if error is not None:
        return _set_password_page(
            request, token, target, error=error, status_code=400, form_token=form_token
        )

    password_hash = await run_in_threadpool(hash_password, password)
    old_token = request.cookies.get(cookie_name(_settings(request), SESSION_COOKIE))
    session_token = await run_in_threadpool(
        _use_link, _sessions(request), token, password_hash, old_token, client_ip_prefix(request)
    )
    if session_token is None:
        return _invalid_link(request)
    return _logged_in_redirect(request, session_token)


# --- Logging --------------------------------------------------------------------------

_INVITE_PATH = re.compile(r"(/invite/)[^\s/?#\"']+")


def _redact(value: str) -> str:
    return _INVITE_PATH.sub(r"\1[redacted]", value)


class RedactInviteTokens(logging.Filter):
    """Replace ``/invite/<token>`` with ``/invite/[redacted]`` in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install_log_redaction() -> None:
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactInviteTokens) for f in logger.filters):
            logger.addFilter(RedactInviteTokens())

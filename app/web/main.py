"""Application factory.

Debug mode is off and unhandled errors return a generic 500 page, so error
responses never expose tracebacks or row data. Request bodies are never
logged. Every response carries the security headers in ``SECURITY_HEADERS``; in
production also ``Strict-Transport-Security`` (``HSTS_HEADERS``).

Settings come from ``get_settings()`` unless passed in (tests pass their own).
Bad settings raise ``SettingsError``, so the app refuses to start. ``app`` (for
``uvicorn app.web.main:app``) is built on first access, so importing this module
reads no settings.

Retention: the app's lifespan runs ``retention.purge_if_due`` once at startup and
then every ``RETENTION_CHECK_SECONDS`` in a background task. The check lives in
the database (an advisory lock plus the day's ``retention_purge`` audit row), so
with several workers, restarts and deploys the purge still runs at most once per
UTC day, and no request ever waits for it. A failure is logged by exception class
only and retried at the next check.

Upload cache: the lifespan also deletes expired entries of ``app.state.upload_cache``
(checked rows, including activation codes) every ``CACHE_SWEEP_SECONDS``, including
those that would expire before the next sweep, so no entry stays in memory longer than
UPLOAD_CACHE_TTL_MINUTES after its last use, even when no request comes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import retention
from app.db.engine import make_engine
from app.mail import Mailer, mailer_from
from app.settings import Settings, check_web_settings, get_settings
from app.upload_cache import UploadCache
from app.web import account, admin, auth, forgot, history, legal, public, team
from app.web.routes import router

RETENTION_CHECK_SECONDS = 60 * 60
CACHE_SWEEP_SECONDS = 60
logger = logging.getLogger(__name__)

STATIC_DIR: Path = Path(__file__).parent / "static"

# Scripts and styles come only from /static: no inline code, no eval, no third parties.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Production only: development runs on plain http://127.0.0.1. Two years, no preload.
HSTS_HEADERS: dict[str, str] = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


def security_headers(settings: Settings) -> dict[str, str]:
    """``SECURITY_HEADERS``, plus ``HSTS_HEADERS`` in production."""
    return SECURITY_HEADERS | HSTS_HEADERS if settings.is_production else dict(SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that adds ``headers`` (default ``SECURITY_HEADERS``) to every
    HTTP response.

    It does not touch the request body, so uploads keep streaming to the route.
    """

    def __init__(self, app: ASGIApp, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self.headers = SECURITY_HEADERS if headers is None else headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _generic_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Return an opaque 500. Never include exception details or request data.

    Starlette sends this from outside the middleware stack, so the security
    headers are set here as well.
    """
    headers = security_headers(request.app.state.settings)
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)


async def healthz() -> PlainTextResponse:
    """GET /healthz: public liveness check (no database access)."""
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


def _purge_if_due(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as db:
        retention.purge_if_due(db, now=datetime.now(UTC))


async def _check_retention(sessions: sessionmaker[Session]) -> None:
    try:
        await run_in_threadpool(_purge_if_due, sessions)
    except Exception as exc:  # never the message: it could include the database URL
        logger.warning("retention purge failed (%s)", type(exc).__name__)


async def _retention_loop(sessions: sessionmaker[Session]) -> None:
    while True:
        await asyncio.sleep(RETENTION_CHECK_SECONDS)
        await _check_retention(sessions)


async def _sweep_upload_cache(cache: UploadCache) -> None:
    """Delete expired upload cache entries every ``CACHE_SWEEP_SECONDS``."""
    while True:
        await asyncio.sleep(CACHE_SWEEP_SECONDS)
        cache.purge_expired(within=CACHE_SWEEP_SECONDS)  # gone before the next sweep


def _lifespan(
    sessions: sessionmaker[Session] | None, run_retention: bool
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        retain = run_retention and sessions is not None
        if retain:
            await _check_retention(sessions)  # at startup, before serving
        tasks = [asyncio.create_task(_sweep_upload_cache(app.state.upload_cache))]
        if retain:
            tasks.append(asyncio.create_task(_retention_loop(sessions)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return lifespan


_FROM_SETTINGS: Any = object()


def create_app(
    settings: Settings | None = None,
    *,
    sessions: sessionmaker[Session] | None = None,
    run_retention: bool = True,
    mailer: Mailer | None = _FROM_SETTINGS,
) -> FastAPI:
    """Build the FastAPI app: debug off, no API docs, security headers, static files,
    auth, routes. ``sessions`` defaults to a sessionmaker on ``DATABASE_URL``.
    ``run_retention`` starts the daily purge with the app (tests turn it off).
    ``mailer`` defaults to the one ``EMAIL_BACKEND`` names (``None``: no email); tests
    pass ``app.mail.FakeMailer``."""
    settings = settings if settings is not None else get_settings()
    check_web_settings(settings)
    if mailer is _FROM_SETTINGS:
        mailer = mailer_from(settings)
    if sessions is None and settings.database_url is not None:  # checked just above
        engine = make_engine(settings.database_url.reveal())
        sessions = sessionmaker(bind=engine, expire_on_commit=False)

    app = FastAPI(
        title="eSIM PDF generator",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(sessions, run_retention),
    )
    app.state.settings = settings
    app.state.sessionmaker = sessions
    app.state.mailer = mailer  # None: EMAIL_BACKEND=none, links are only shown
    # Checked rows between /validate and /generate, in this process's memory only.
    app.state.upload_cache = UploadCache(
        max_bytes=settings.upload_cache_mb * 1024 * 1024,
        ttl_seconds=settings.upload_cache_ttl_minutes * 60,
    )
    app.add_exception_handler(Exception, _generic_error)
    app.add_exception_handler(auth.AuthError, auth.handle_auth_error)
    app.add_middleware(SecurityHeadersMiddleware, headers=security_headers(settings))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/healthz", healthz, methods=["GET"], response_model=None)
    app.include_router(auth.router)
    app.include_router(forgot.router)
    app.include_router(account.router)
    app.include_router(public.router)
    app.include_router(legal.router)
    app.include_router(admin.router)
    app.include_router(team.router)
    app.include_router(history.router)
    app.include_router(router)
    auth.install_log_redaction()
    return app


def __getattr__(name: str) -> FastAPI:
    """Build ``app`` on first access (``uvicorn app.web.main:app``)."""
    if name == "app":
        built = create_app()
        globals()["app"] = built
        return built
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

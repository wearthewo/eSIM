"""Application factory.

Debug mode is off and unhandled errors return a generic 500 page, so error
responses never expose tracebacks or row data. Request bodies are never
logged. Every response carries the security headers in ``SECURITY_HEADERS``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.web.routes import router

STATIC_DIR: Path = Path(__file__).parent / "static"

# Scripts and styles come only from /static: no inline code, no eval, no third parties.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
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


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that adds ``SECURITY_HEADERS`` to every HTTP response.

    It does not touch the request body, so uploads keep streaming to the route.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _generic_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Return an opaque 500. Never include exception details or request data.

    Starlette sends this from outside the middleware stack, so the security
    headers are set here as well.
    """
    return PlainTextResponse("Internal Server Error", status_code=500, headers=SECURITY_HEADERS)


def create_app() -> FastAPI:
    """Build the FastAPI app: debug off, no API docs, security headers, static files, routes."""
    app = FastAPI(
        title="eSIM PDF generator",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_exception_handler(Exception, _generic_error)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()

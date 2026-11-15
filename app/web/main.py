"""Application factory.

Debug mode is off and unhandled errors return a generic 500 page, so error
responses never expose tracebacks or row data. Request bodies are never
logged.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.web.routes import router

STATIC_DIR: Path = Path(__file__).parent / "static"


async def _generic_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Return an opaque 500. Never include exception details or request data."""
    return PlainTextResponse("Internal Server Error", status_code=500)


def create_app() -> FastAPI:
    """Build the FastAPI app: debug off, generic 500 handler, static files, routes."""
    app = FastAPI(title="eSIM PDF generator", debug=False)
    app.add_exception_handler(Exception, _generic_error)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app


app = create_app()

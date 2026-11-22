"""The shared Jinja2 environment.

Every template gets ``current_user`` and ``csrf_token`` (both ``None`` when not
logged in) and ``pending_requests`` (superadmins only, else 0), set on
``request.state`` by ``app.web.auth.require_user``.

Autoescaping is on for every template (Starlette's ``Jinja2Templates`` default,
checked by the tests). Templates never use ``|safe`` and never put user input in
``href``, ``src`` or ``action`` attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.numbers import thousands
from app.localtime import format_athens

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"


def _auth_context(request: Request) -> dict[str, Any]:
    return {
        "current_user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", None),
        "pending_requests": getattr(request.state, "pending_requests", 0),
    }


def _utc(value: datetime | None) -> str:
    """``2026-11-16 09:30 UTC``; a dash for none."""
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_auth_context])
templates.env.filters["utc"] = _utc
templates.env.filters["athens"] = format_athens  # dd/mm/yyyy HH:mm, Greek time
# Every number shown in a page: 10,000 (app/core/numbers.py). Not in attributes.
templates.env.filters["thousands"] = thousands

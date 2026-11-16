"""The shared Jinja2 environment.

Every template gets ``current_user`` and ``csrf_token`` (both ``None`` when not
logged in), set on ``request.state`` by ``app.web.auth.require_user``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"


def _auth_context(request: Request) -> dict[str, Any]:
    return {
        "current_user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", None),
    }


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_auth_context])

"""app.core must never import the web layer, the database layer or the settings.

Forbidden: FastAPI, Starlette, app.web, app.db, app.settings, and the database
libraries (SQLAlchemy, Alembic, psycopg).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parents[2] / "app" / "core"
FORBIDDEN_PACKAGES = ("fastapi", "starlette", "sqlalchemy", "alembic", "psycopg")
FORBIDDEN_APP_MODULES = ("app.web", "app.db", "app.settings")


def _imports(source: str) -> list[str]:
    """Every module ``source`` imports. ``from app import db`` is reported as ``app.db``."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(source: str) -> list[str]:
    return sorted(
        {
            name
            for name in _imports(source)
            if name.split(".")[0] in FORBIDDEN_PACKAGES
            or any(name == m or name.startswith(m + ".") for m in FORBIDDEN_APP_MODULES)
        }
    )


@pytest.mark.parametrize("path", sorted(CORE_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_core_has_no_web_or_db_imports(path: Path) -> None:
    bad = _forbidden(path.read_text(encoding="utf-8"))
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize(
    "source",
    [
        "import fastapi",
        "from starlette.requests import Request",
        "import sqlalchemy.orm",
        "from psycopg import sql",
        "import alembic",
        "import app.db.models",
        "from app.db import models",
        "from app import db",
        "from app import settings",
        "from app.settings import get_settings",
        "from app.web.routes import router",
    ],
)
def test_detector_catches_forbidden_imports(source: str) -> None:
    assert _forbidden(source), source


@pytest.mark.parametrize(
    "source",
    ["import io", "from app.core.models import Branding", "import app.core.qr", "import dbm"],
)
def test_detector_allows_other_imports(source: str) -> None:
    assert not _forbidden(source), source

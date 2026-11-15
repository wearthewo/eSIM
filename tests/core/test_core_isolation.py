"""app.core must never import FastAPI, Starlette, or app.web."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parents[2] / "app" / "core"
FORBIDDEN = ("fastapi", "starlette", "app.web")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(CORE_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_core_has_no_web_imports(path: Path) -> None:
    bad = [n for n in _imports(path) if n.split(".")[0] in FORBIDDEN or n.startswith("app.web")]
    assert not bad, f"{path.name} imports {bad}"

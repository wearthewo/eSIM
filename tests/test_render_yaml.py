"""render.yaml (Render Blueprint) keeps what production needs (CLAUDE.md, README
"Deploy to Render"). PyYAML comes with uvicorn[standard]."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]

START_COMMAND = (
    "python -m alembic upgrade head && python -m app.admin bootstrap && "
    "uvicorn app.web.main:app --host 0.0.0.0 --port $PORT --workers 1 "
    "--proxy-headers --forwarded-allow-ips '*'"
)


@pytest.fixture(scope="module")
def blueprint() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def web(blueprint: dict[str, Any]) -> dict[str, Any]:
    [service] = blueprint["services"]
    return service


@pytest.fixture(scope="module")
def env(web: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {var["key"]: var for var in web["envVars"]}


def test_database(blueprint: dict[str, Any], web: dict[str, Any]) -> None:
    [db] = blueprint["databases"]
    assert db["plan"] == "free"  # upgrade before day 30 (see the comment in render.yaml)
    assert db["region"] == web["region"]  # private network
    assert db["ipAllowList"] == []  # no connections from the internet


def test_web_service(web: dict[str, Any]) -> None:
    assert web["type"] == "web"
    assert web["runtime"] == "python"
    assert web["buildCommand"] == "pip install ."
    assert web["startCommand"] == START_COMMAND
    assert web["healthCheckPath"] == "/healthz"


def test_database_url_comes_from_the_database(
    blueprint: dict[str, Any], env: dict[str, dict[str, Any]]
) -> None:
    [db] = blueprint["databases"]
    assert env["DATABASE_URL"]["fromDatabase"] == {
        "name": db["name"],
        "property": "connectionString",
    }


def test_keys_are_generated_by_render(env: dict[str, dict[str, Any]]) -> None:
    for name in ("SECRET_KEY", "FINGERPRINT_KEY", "MFA_KEY"):
        assert env[name] == {"key": name, "generateValue": True}


def test_secrets_and_per_deploy_values_are_not_in_the_file(
    env: dict[str, dict[str, Any]],
) -> None:
    for name in (
        "PUBLIC_BASE_URL",
        "BOOTSTRAP_SUPERADMIN_EMAIL",
        "RESEND_API_KEY",
        "EMAIL_FROM",
        "ADMIN_NOTIFY_EMAIL",
    ):
        assert env[name] == {"key": name, "sync": False}


def test_fixed_values(env: dict[str, dict[str, Any]]) -> None:
    values = {k: v["value"] for k, v in env.items() if "value" in v}
    python_version = values.pop("PYTHON_VERSION")
    assert values == {
        "ENV": "production",
        "EMAIL_BACKEND": "none",
        "GENERATE_SLOTS": "1",
        "UPLOAD_CACHE_MB": "100",
        "GENERATE_WAIT_SECONDS": "60",
    }
    # Render needs a full version here; .python-version names the minor version.
    local = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert python_version.count(".") == 2
    assert python_version.startswith(f"{local}.")

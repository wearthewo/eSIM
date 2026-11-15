from __future__ import annotations

from fastapi.testclient import TestClient

from app.web import routes
from app.web.main import create_app
from tests.conftest import not_implemented


def test_index_renders() -> None:
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="file"' in response.text


def test_app_debug_off() -> None:
    assert create_app().debug is False


@not_implemented
def test_get_branding() -> None:
    routes.get_branding()

from __future__ import annotations

from app.core import pipeline
from app.core.models import Branding
from tests.conftest import not_implemented


@not_implemented
def test_process_upload(fixture_bytes) -> None:
    pipeline.process_upload(
        fixture_bytes("valid_minimal.csv"),
        "valid_minimal.csv",
        Branding(company_name="Example Telecom (test)"),
    )

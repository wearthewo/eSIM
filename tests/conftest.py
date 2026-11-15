"""Shared fixtures. All data is synthetic.

ICCIDs use the fake ``8999`` prefix and activation codes point at
``smdp.example.invalid``. Real activation codes must never be used.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"

not_implemented = pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="stub")


@pytest.fixture
def fixture_bytes() -> Callable[[str], bytes]:
    """Return a loader: ``fixture_bytes("valid_minimal.csv") -> bytes``."""

    def _load(name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    return _load


# --- Generated at test time (stubs until implemented) -------------------------


@pytest.fixture
def too_many_rows_csv() -> bytes:
    """CSV with MAX_ROWS + 1 (501) valid synthetic rows."""
    raise NotImplementedError


@pytest.fixture
def valid_xlsx() -> bytes:
    """XLSX with identifiers stored as text cells."""
    raise NotImplementedError


@pytest.fixture
def numeric_iccid_xlsx() -> bytes:
    """XLSX whose ICCID cell is stored as a number (must be rejected, not cast)."""
    raise NotImplementedError


@pytest.fixture
def extra_sheets_and_columns_xlsx() -> bytes:
    """XLSX with data on the first sheet, extra sheets, and unknown columns."""
    raise NotImplementedError


@pytest.fixture
def corrupt_xlsx() -> bytes:
    """Arbitrary bytes that are not a valid XLSX archive."""
    raise NotImplementedError


@pytest.fixture
def oversize_upload() -> bytes:
    """Payload just over the 5 MB upload cap."""
    raise NotImplementedError

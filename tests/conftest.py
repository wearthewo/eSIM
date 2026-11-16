"""Shared fixtures. All data is synthetic.

ICCIDs use the fake ``8999`` prefix and activation codes point at
``smdp.example.invalid``. Real activation codes must never be used.
"""

from __future__ import annotations

import io
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


# --- Synthetic data helpers ---------------------------------------------------


def luhn_check_digit(payload: str) -> str:
    """Return the Luhn check digit for a string of digits."""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def synthetic_iccid(n: int) -> str:
    """19-digit synthetic ICCID with the fake ``8999`` prefix and a valid Luhn digit."""
    payload = f"8999{n:014d}"
    return payload + luhn_check_digit(payload)


def synthetic_csv(count: int) -> bytes:
    """CSV with ``count`` valid synthetic rows."""
    lines = ["iccid,activation_code"]
    lines += [
        f"{synthetic_iccid(i)},LPA:1$smdp.example.invalid$TEST-{i:04d}-AAAA-BBBB"
        for i in range(1, count + 1)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_xlsx(*sheets: list[list[object]]) -> bytes:
    """Build an XLSX in memory. Each argument is one sheet's rows (first = first sheet).

    Python ``str`` values become text cells and ``int``/``float`` values become
    numeric cells, so tests control exactly how each identifier is stored.
    """
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for i, rows in enumerate(sheets or ([],)):
        ws = wb.create_sheet(title=f"Sheet{i + 1}")
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Generated at test time ----------------------------------------------------


@pytest.fixture
def xlsx_bytes() -> Callable[..., bytes]:
    """Return the ``build_xlsx`` builder: ``xlsx_bytes(rows_sheet1, rows_sheet2, ...)``."""
    return build_xlsx


@pytest.fixture
def too_many_rows_csv() -> bytes:
    """CSV with MAX_ROWS + 1 (501) valid synthetic rows."""
    return synthetic_csv(501)


@pytest.fixture
def valid_xlsx() -> bytes:
    """XLSX with identifiers stored as text cells."""
    return build_xlsx(
        [
            ["iccid", "activation_code", "confirmation_code", "imsi", "msisdn", "customer_name"],
            [
                "8999000000000000013",
                "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB",
                "CONF0001",
                "001010000000001",
                "+306900000001",
                "Test Customer 1",
            ],
            [
                "8999000000000000021",
                "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB",
                None,
                "001010000000002",
                "0030690000002",
                "Test Customer 2",
            ],
        ]
    )


@pytest.fixture
def numeric_iccid_xlsx() -> bytes:
    """XLSX whose ICCID cell is stored as a number (must be rejected, not cast)."""
    return build_xlsx(
        [
            ["iccid", "activation_code"],
            ["8999000000000000013", "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"],
            [8999000000000000021, "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB"],
        ]
    )


@pytest.fixture
def extra_sheets_and_columns_xlsx() -> bytes:
    """XLSX with data on the first sheet, extra sheets, and unknown columns."""
    return build_xlsx(
        [
            ["Notes", "ICCID", "Internal Ref", "Activation_Code", "Plan_Name"],
            [
                "ignore me",
                "8999000000000000013",
                12345,
                "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB",
                "Test Plan",
            ],
        ],
        [
            ["iccid", "activation_code"],
            ["8999000000000000021", "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB"],
        ],
        [["something else entirely"]],
    )


@pytest.fixture
def corrupt_xlsx() -> bytes:
    """Arbitrary bytes that are not a valid XLSX archive."""
    return b"PK\x03\x04 this is not really a zip archive \x00\xff\xfe"


OVERSIZE_CAP_BYTES = 5 * 1024 * 1024  # app.web.routes.MAX_UPLOAD_BYTES (asserted in tests/web)


@pytest.fixture
def oversize_upload() -> bytes:
    """Payload just over the 5 MB upload cap: a valid CSV start, padded to cap + 1 bytes."""
    head = synthetic_csv(1)
    return head + b"#" * (OVERSIZE_CAP_BYTES + 1 - len(head))

"""Parse uploaded CSV/XLSX bytes into ``EsimRecord`` objects.

Everything is read from memory. Uploads are never written to disk. Every cell
is kept as ``str``, and identifiers are never coerced to numbers.

Accepted activation forms (at least one is required):
  * ``activation_code``: the full LPA string, e.g. ``LPA:1$host$MATCHING-ID``
  * ``smdp_address`` + ``matching_id``: combined via ``build_activation_code``

Never log activation codes, confirmation codes, or file contents.
"""

from __future__ import annotations

from app.core.models import EsimRecord

MAX_ROWS = 500
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".xlsx"})
REQUIRED_COLUMNS: frozenset[str] = frozenset({"iccid"})
ACTIVATION_FULL_COLUMN = "activation_code"
ACTIVATION_SPLIT_COLUMNS: tuple[str, str] = ("smdp_address", "matching_id")
OPTIONAL_COLUMNS: frozenset[str] = frozenset(
    {"confirmation_code", "imsi", "msisdn", "customer_name", "plan_name"}
)


def parse_upload(data: bytes, filename: str) -> list[EsimRecord]:
    """Pick the parser from the file extension and return the parsed records.

    Raises ``ParseError`` for unsupported extensions or unreadable content.
    """
    raise NotImplementedError


def parse_csv(data: bytes) -> list[EsimRecord]:
    """Parse CSV bytes with the stdlib ``csv`` module.

    Decodes UTF-8 (BOM allowed), detects ``,`` or ``;`` delimiters, trims
    whitespace, skips fully blank rows, and enforces ``MAX_ROWS``. All cells stay
    ``str``. Raises ``ParseError``.
    """
    raise NotImplementedError


def parse_xlsx(data: bytes) -> list[EsimRecord]:
    """Parse the first worksheet of an XLSX workbook with openpyxl.

    Opens it with ``read_only=True, data_only=True``. If an identifier cell
    (ICCID/IMSI/MSISDN) is stored as a number, it raises ``ParseError`` rather
    than converting, because float storage may already have lost digits.
    Enforces ``MAX_ROWS``.
    """
    raise NotImplementedError


def normalize_headers(headers: list[str]) -> dict[str, int]:
    """Map canonical (lower-cased, trimmed) column names to their indices.

    Unknown columns are ignored. Raises ``ParseError`` on duplicate headers,
    on a missing ``iccid``, or when neither activation form is present.
    """
    raise NotImplementedError


def build_activation_code(smdp_address: str, matching_id: str) -> str:
    """Return ``LPA:1$<smdp_address>$<matching_id>``.

    The confirmation code is never included.
    """
    raise NotImplementedError

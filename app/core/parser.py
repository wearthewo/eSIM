"""Parse uploaded CSV/XLSX bytes into ``EsimRecord`` objects.

Everything is read from memory. Uploads are never written to disk. Every cell
is kept as ``str``, and identifiers are never coerced to numbers.

Accepted activation forms (at least one is required):
  * ``activation_code``: the full LPA string, e.g. ``LPA:1$host$MATCHING-ID``
  * ``smdp_address`` + ``matching_id``: combined via ``build_activation_code``

Light normalization happens here (the validator stays strict):
  * every cell is trimmed; empty optional fields become ``None``
  * a case-insensitive ``lpa:1$`` prefix becomes ``LPA:1$`` (nothing else changes)
  * MSISDN loses spaces, dashes and parentheses (a leading ``+`` is kept)
  * ICCID and IMSI are trimmed only

Never log activation codes, confirmation codes, or file contents. ``ParseError``
messages name rows and columns only, never cell values.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Callable, Iterable, Iterator, Sequence

from app.core.models import EsimRecord, ParseError

MAX_ROWS = 500
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".xlsx"})
REQUIRED_COLUMNS: frozenset[str] = frozenset({"iccid"})
ACTIVATION_FULL_COLUMN = "activation_code"
ACTIVATION_SPLIT_COLUMNS: tuple[str, str] = ("smdp_address", "matching_id")
OPTIONAL_COLUMNS: frozenset[str] = frozenset(
    {"confirmation_code", "imsi", "msisdn", "customer_name", "plan_name"}
)
KNOWN_COLUMNS: frozenset[str] = (
    REQUIRED_COLUMNS
    | {ACTIVATION_FULL_COLUMN}
    | frozenset(ACTIVATION_SPLIT_COLUMNS)
    | OPTIONAL_COLUMNS
)
# XLSX cells in these columns must be stored as text (numbers may have lost digits).
IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {"iccid", "imsi", "msisdn", "activation_code", "matching_id", "confirmation_code"}
)

LPA_PREFIX = "LPA:1$"
_MSISDN_SEPARATORS = re.compile(r"[ \-()]")

Cell = str | None


def parse_upload(data: bytes, filename: str) -> list[EsimRecord]:
    """Pick the parser from the file extension and return the parsed records.

    Raises ``ParseError`` for unsupported extensions or unreadable content.
    """
    extension = os.path.splitext(filename or "")[1].lower()
    if extension == ".csv":
        return parse_csv(data)
    if extension == ".xlsx":
        return parse_xlsx(data)
    raise ParseError("Unsupported file type. Upload a .csv or .xlsx file.")


def parse_csv(data: bytes) -> list[EsimRecord]:
    """Parse CSV bytes with the stdlib ``csv`` module.

    Decodes UTF-8 (BOM allowed), detects ``,`` or ``;`` delimiters, trims
    whitespace, skips rows whose known columns are all empty (values in unknown
    columns are ignored, as in ``parse_xlsx``), and enforces ``MAX_ROWS``. All cells stay
    ``str``. Raises ``ParseError``.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError("The CSV file is not valid UTF-8 text.") from None

    if not text.strip():
        raise ParseError("The file is empty: no header row found.")

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_detect_delimiter(text))
    try:
        rows = list(reader)
    except csv.Error:
        raise ParseError("The CSV file could not be read.") from None

    header = [cell.strip() for cell in rows[0]]
    columns = normalize_headers(header)
    numbered = ((number, [_trim(c) for c in row]) for number, row in enumerate(rows[1:], start=2))
    return _build_records(numbered, columns)


def parse_xlsx(data: bytes) -> list[EsimRecord]:
    """Parse the first worksheet of an XLSX workbook with openpyxl.

    Opens it with ``read_only=True, data_only=True``. If an identifier cell
    (ICCID, IMSI, MSISDN, activation code, matching ID, confirmation code) is
    stored as anything other than text, it raises ``ParseError`` naming the row
    and column rather than converting, because float storage may already have
    lost digits. Enforces ``MAX_ROWS``.

    Rows are streamed: reading stops as soon as row ``MAX_ROWS + 1`` is seen,
    and only columns that map to known headers are read. Values in unknown
    columns are never looked at, so a row with data only there counts as blank.
    """
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ParseError("The XLSX file is corrupt or not a valid workbook.") from None

    try:
        if not workbook.worksheets:
            raise ParseError("The workbook has no worksheets.")
        sheet = workbook.worksheets[0]

        header_rows = list(_guard(sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        if not header_rows:
            raise ParseError("The first worksheet is empty: no header row found.")
        header = ["" if v is None else str(v).strip() for v in header_rows[0]]
        columns = normalize_headers(header)

        # Only the columns up to the last known header are read, and only known ones are kept.
        width = max(columns.values()) + 1
        names_by_index = {index: name for name, index in columns.items()}
        data_rows = _guard(sheet.iter_rows(min_row=2, max_col=width, values_only=True))
        numbered = (
            (number, _xlsx_cells(number, row, names_by_index, width))
            for number, row in enumerate(data_rows, start=2)
        )
        return _build_records(numbered, columns)
    finally:
        workbook.close()


def normalize_headers(headers: list[str]) -> dict[str, int]:
    """Map canonical (lower-cased, trimmed) column names to their indices.

    Unknown columns are ignored. Raises ``ParseError`` on duplicate headers,
    on a missing ``iccid``, or when neither activation form is present.
    """
    columns: dict[str, int] = {}
    for index, raw in enumerate(headers):
        name = (raw or "").strip().lower()
        if name not in KNOWN_COLUMNS:
            continue
        if name in columns:
            raise ParseError(f"Column '{name}' appears more than once in the header row.")
        columns[name] = index

    missing = sorted(REQUIRED_COLUMNS - columns.keys())
    if missing:
        raise ParseError(f"Missing required column: {', '.join(missing)}.")

    has_full = ACTIVATION_FULL_COLUMN in columns
    has_split = all(c in columns for c in ACTIVATION_SPLIT_COLUMNS)
    if not (has_full or has_split):
        raise ParseError(
            "Missing activation columns: provide 'activation_code', "
            "or both 'smdp_address' and 'matching_id'."
        )
    return columns


def build_activation_code(smdp_address: str, matching_id: str) -> str:
    """Return ``LPA:1$<smdp_address>$<matching_id>``.

    The confirmation code is never included.
    """
    return f"{LPA_PREFIX}{smdp_address}${matching_id}"


# --- internals ----------------------------------------------------------------


def _detect_delimiter(text: str) -> str:
    """Choose ``;`` or ``,`` by counting them in the header line."""
    header_line = text.lstrip("\r\n").splitlines()[0]
    return ";" if header_line.count(";") > header_line.count(",") else ","


def _guard(rows: Iterable[tuple[object, ...]]) -> Iterator[tuple[object, ...]]:
    """Re-raise errors from openpyxl's lazy XML reading as a generic ``ParseError``."""
    iterator = iter(rows)
    while True:
        try:
            row = next(iterator)
        except StopIteration:
            return
        except Exception:
            raise ParseError("The XLSX file is corrupt or not a valid workbook.") from None
        yield row


def _xlsx_cells(
    number: int, row: Sequence[object], names_by_index: dict[int, str], width: int
) -> list[Cell]:
    """Return a positional row of trimmed text cells; unknown columns are ``None``.

    Identifier columns must hold text: anything else raises ``ParseError`` naming
    the row and column (never the value).
    """
    out: list[Cell] = [None] * width
    for index, name in names_by_index.items():
        value = row[index] if index < len(row) else None
        if value is None or isinstance(value, str):
            out[index] = _trim(value)
        elif name in IDENTIFIER_COLUMNS:
            raise ParseError(
                f"Row {number}, column '{name}': the cell is stored as a number or date, "
                "not text. Format the column as Text and re-enter the values."
            )
        else:
            out[index] = _trim(str(value))
    return out


def _trim(value: str | None) -> Cell:
    """Strip whitespace; empty strings become ``None``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _build_records(
    rows: Iterable[tuple[int, list[Cell]]], columns: dict[str, int]
) -> list[EsimRecord]:
    """Turn trimmed rows into records, skipping blank rows and enforcing ``MAX_ROWS``.

    A row is blank when every known column is empty; values in unknown columns
    are ignored, so CSV and XLSX skip exactly the same rows.
    """
    indices = tuple(columns.values())
    records: list[EsimRecord] = []
    for number, cells in rows:
        if all(i >= len(cells) or cells[i] is None for i in indices):
            continue
        if len(records) >= MAX_ROWS:
            raise ParseError(f"Too many rows: the limit is {MAX_ROWS} data rows per upload.")
        records.append(_build_record(number, cells, columns))
    return records


def _build_record(number: int, cells: list[Cell], columns: dict[str, int]) -> EsimRecord:
    def get(name: str) -> Cell:
        index = columns.get(name)
        if index is None or index >= len(cells):
            return None
        return cells[index]

    msisdn = get("msisdn")
    if msisdn is not None:
        msisdn = _MSISDN_SEPARATORS.sub("", msisdn) or None

    return EsimRecord(
        row_number=number,
        iccid=get("iccid") or "",
        activation_code=_activation_code(get),
        confirmation_code=get("confirmation_code"),
        imsi=get("imsi"),
        msisdn=msisdn,
        customer_name=get("customer_name"),
        plan_name=get("plan_name"),
    )


def _activation_code(get: Callable[[str], Cell]) -> str:
    """Full code wins; otherwise build from the split pair; otherwise ``""``.

    An empty result is left for the validator to report as missing.
    """
    full = get(ACTIVATION_FULL_COLUMN)
    if full:
        if full[: len(LPA_PREFIX)].lower() == LPA_PREFIX.lower():
            full = LPA_PREFIX + full[len(LPA_PREFIX) :]
        return full
    smdp_address, matching_id = (get(c) for c in ACTIVATION_SPLIT_COLUMNS)
    if smdp_address or matching_id:
        return build_activation_code(smdp_address or "", matching_id or "")
    return ""

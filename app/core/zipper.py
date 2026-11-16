"""In-memory ZIP assembly plus the manifest and the mail-merge list.

PDFs are already compressed, so entries use ``ZIP_STORED``. The manifest
lists only ``row_number``, ``iccid`` and ``pdf_filename``. ``mailmerge.csv``
(only for uploads with an ``email`` column) lists ``email``, ``customer_name``,
``plan_name``, ``iccid`` and ``pdf_filename``. Neither file ever contains
activation codes, matching IDs or confirmation codes.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable

MANIFEST_NAME = "manifest.csv"
MANIFEST_COLUMNS: tuple[str, str, str] = ("row_number", "iccid", "pdf_filename")
MAILMERGE_NAME = "mailmerge.csv"
MAILMERGE_COLUMNS: tuple[str, str, str, str, str] = (
    "email",
    "customer_name",
    "plan_name",
    "iccid",
    "pdf_filename",
)
CSV_INJECTION_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
FALLBACK_STEM = "file"
# Earliest timestamp a ZIP entry can hold; fixed so identical input gives identical bytes.
FIXED_DATE_TIME: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)

_SEPARATORS = re.compile(r"[/\\]")
# Characters Windows refuses in filenames (separators are handled separately).
_RESERVED = re.compile(r'[<>:"|?*]')
_FILE_MODE = 0o100644  # regular file, rw-r--r--
_UNIX = 3


def sanitize_filename(name: str) -> str:
    """Return a safe archive member name.

    Keeps only the last path component, so separators, drive letters and
    ``..`` segments cannot escape the extraction directory. Removes control and
    other non-printing characters, Windows-reserved characters, any remaining
    ``..``, and leading dots. Unicode letters are kept (``build_zip`` sets the
    UTF-8 flag). If nothing usable remains the stem becomes ``"file"``; the
    extension is kept.
    """
    name = unicodedata.normalize("NFC", name or "")
    parts = [p for p in _SEPARATORS.split(name) if p not in ("", ".", "..")]
    last = parts[-1] if parts else ""

    stem, dot, ext = last.rpartition(".")
    if not dot or not ext or not ext.isalnum():
        stem, ext = last, ""
    stem = _clean(stem)
    ext = _clean(ext)
    return (stem or FALLBACK_STEM) + (f".{ext}" if ext else "")


def _clean(text: str) -> str:
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    text = _RESERVED.sub("", text)
    while ".." in text:
        text = text.replace("..", "")
    return text.lstrip(". ").rstrip(". ")


def escape_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection.

    Prefixes ``'`` to any cell starting with one of ``CSV_INJECTION_PREFIXES``
    (``= + - @ \\t \\r``).
    """
    return "'" + value if value.startswith(CSV_INJECTION_PREFIXES) else value


def build_manifest(entries: Iterable[tuple[int, str, str]]) -> bytes:
    """Render ``(row_number, iccid, pdf_filename)`` rows as UTF-8 CSV bytes.

    Starts with a BOM so Excel detects UTF-8 (Greek names display correctly).
    Writes a header row and escapes every cell. Secrets are never included.
    """
    return _csv_bytes(MANIFEST_COLUMNS, entries)


def build_mailmerge(entries: Iterable[tuple[str, str, str, str, str]]) -> bytes:
    """Render ``(email, customer_name, plan_name, iccid, pdf_filename)`` rows as UTF-8 CSV bytes.

    For a mail merge run by the user; the server never sends mail. Starts with
    a BOM, writes a header row and escapes every cell, like the manifest.
    Secrets are never included.
    """
    return _csv_bytes(MAILMERGE_COLUMNS, entries)


def _csv_bytes(header: Iterable[str], rows: Iterable[Iterable[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([escape_csv_cell(str(cell)) for cell in row])
    return buf.getvalue().encode("utf-8-sig")


def build_zip(
    files: Iterable[tuple[str, bytes]],
    manifest: bytes | None = None,
    mailmerge: bytes | None = None,
) -> bytes:
    """Write ``(name, data)`` pairs (plus optional manifest and mail-merge list) into a ZIP.

    Uses ``ZIP_STORED`` and fixed timestamps (deterministic output), sanitizes
    names, sets the UTF-8 flag for non-ASCII names, and raises ``ValueError``
    on duplicate names after sanitizing (compared case-insensitively, since
    Windows and macOS would overwrite one with the other on extraction).
    """
    entries = [(sanitize_filename(name), data) for name, data in files]
    if manifest is not None:
        entries.append((MANIFEST_NAME, manifest))
    if mailmerge is not None:
        entries.append((MAILMERGE_NAME, mailmerge))

    seen: set[str] = set()
    for name, _ in entries:
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate file name in archive: {name}")
        seen.add(key)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = _UNIX
            info.external_attr = _FILE_MODE << 16
            zf.writestr(info, data)
    return buf.getvalue()

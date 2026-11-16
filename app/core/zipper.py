"""In-memory ZIP assembly plus the manifest and the mail-merge list.

PDFs are already compressed, so entries use ``ZIP_STORED``. ``manifest.xlsx``
lists only ``row_number``, ``iccid`` and ``pdf_filename``. ``mailmerge.xlsx``
(only for uploads with an ``email`` column) lists ``email``, ``customer_name``,
``plan_name``, ``iccid`` and ``pdf_filename``. Neither file ever contains
activation codes, matching IDs or confirmation codes. Both are Excel files in
which every cell is text (see ``app.core.sheets``).
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Iterable

from app.core.sheets import FIXED_DATE_TIME, text_xlsx

MANIFEST_NAME = "manifest.xlsx"
MANIFEST_COLUMNS: tuple[str, str, str] = ("row_number", "iccid", "pdf_filename")
MAILMERGE_NAME = "mailmerge.xlsx"
MAILMERGE_COLUMNS: tuple[str, str, str, str, str] = (
    "email",
    "customer_name",
    "plan_name",
    "iccid",
    "pdf_filename",
)
FALLBACK_STEM = "file"

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


def build_manifest(entries: Iterable[tuple[int, str, str]]) -> bytes:
    """Render ``(row_number, iccid, pdf_filename)`` rows as ``manifest.xlsx`` bytes.

    A bold, frozen header row, then one row per PDF; every cell is text (the row
    number too). Secrets are never included.
    """
    return text_xlsx(MANIFEST_COLUMNS, entries, title="manifest")


def build_mailmerge(entries: Iterable[tuple[str, str, str, str, str]]) -> bytes:
    """Render ``(email, customer_name, plan_name, iccid, pdf_filename)`` rows as
    ``mailmerge.xlsx`` bytes.

    For a mail merge run by the user; the server never sends mail. Same format
    as the manifest. Secrets are never included.
    """
    return text_xlsx(MAILMERGE_COLUMNS, entries, title="mailmerge")


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

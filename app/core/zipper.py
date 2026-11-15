"""In-memory ZIP assembly plus the manifest.

PDFs are already compressed, so entries use ``ZIP_STORED``. The manifest
lists only ``row_number``, ``iccid`` and ``pdf_filename``. It never contains
activation or confirmation codes.
"""

from __future__ import annotations

from collections.abc import Iterable

MANIFEST_NAME = "manifest.csv"
MANIFEST_COLUMNS: tuple[str, str, str] = ("row_number", "iccid", "pdf_filename")
CSV_INJECTION_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def sanitize_filename(name: str) -> str:
    """Return a safe archive member name.

    Strips path separators and ``..``, and drops control characters. Falls back
    to a safe ASCII name if nothing usable remains. Non-ASCII letters may be
    kept, because ``build_zip`` sets the UTF-8 flag.
    """
    raise NotImplementedError


def escape_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection.

    Prefixes ``'`` to any cell starting with one of ``CSV_INJECTION_PREFIXES``
    (``= + - @ \\t \\r``).
    """
    raise NotImplementedError


def build_manifest(entries: Iterable[tuple[int, str, str]]) -> bytes:
    """Render ``(row_number, iccid, pdf_filename)`` rows as UTF-8 CSV bytes.

    Writes a header row and escapes every cell. Secrets are never included.
    """
    raise NotImplementedError


def build_zip(files: Iterable[tuple[str, bytes]], manifest: bytes | None = None) -> bytes:
    """Write ``(name, data)`` pairs (plus optional manifest) into an in-memory ZIP.

    Uses ``ZIP_STORED``, sanitizes names, sets the UTF-8 flag for non-ASCII
    names, and raises ``ValueError`` on duplicate names.
    """
    raise NotImplementedError

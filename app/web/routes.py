"""HTTP routes: the upload form, the Excel template, HTMX validation, and ZIP generation.

All of them require a logged-in user (``app.web.auth.require_user``).

Uploads are read from the request stream straight into memory with per-part caps
(``read_capped``) and dropped when the response is sent. Uploads never go to disk.
Starlette's own form parser is not used because it spools file parts over 1 MB to a
temporary file.

Batches (``app.batches``): /validate also checks every row against the codes other
batches of the organization generated in the last 12 months (HMAC fingerprints). A file
that passes gets a batch row (metadata only: name, counts, status), and its id goes into
the page's form. Every /generate request must send that id; a batch of another
organization, or one that no longer exists, gives 404. Each generated part records its
rows' fingerprints and is counted once in ``parts_downloaded`` and in the usage table.
Users without an organization (a superadmin) cannot generate.

Large orders are generated in parts of the batch's ``chunk_rows`` rows. The page asks
for one part at a time. /validate keeps the checked rows of the batch in process memory
(``app.upload_cache``, never on disk), so a part request needs only the batch id and the
part number: no file, no parse, no whole-file check; the part's rows are checked for
duplicates again inside ``batches.record_part``. When the rows are not cached (expired,
evicted, server restarted, another worker) a request without the file gets 428 with the
``X-Esim-Upload-Needed`` header, and the page sends the same part again with the file:
that request is checked in full, as before the cache, and fills the cache again. The
cached rows are dropped when the batch is completed, failed or voided.

Daily quota (``batches.check_quota``): /validate refuses a file whose rows still to
generate would take today's usage over the organization's quota, before a batch is made
(429). /generate checks a part not generated yet before it waits for a job slot, and
``batches.record_part`` checks again under the organization's lock, so two parts at once
cannot both pass. A part downloaded again never counts and is never refused. Every
refusal writes a ``quota_exceeded`` audit row.

Never log request bodies, uploaded filenames, or cell values.
"""

from __future__ import annotations

import asyncio
import os
import re
import struct
import unicodedata
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.orm import Session, sessionmaker

from app import batches
from app.auth.accounts import CurrentUser, IpPrefix
from app.batches import Continuation, DailyUsage, UsedSummary
from app.core import pipeline, sheets
from app.core.models import Branding, EsimRecord, ParseError, ValidationReport
from app.core.parser import MAX_ROWS, SUPPORTED_EXTENSIONS, ParsedUpload
from app.core.pipeline import PartError, check_upload
from app.db.models import Batch, BatchStatus
from app.settings import DEFAULT_GENERATE_SLOTS, DEFAULT_GENERATE_WAIT_SECONDS
from app.upload_cache import CachedUpload, UploadCache
from app.web.auth import client_ip_prefix, require_user, sessions_of, utc_now
from app.web.templating import templates

# Product limits. Settings (MAX_UPLOAD_MB, MAX_ROWS) default to these; only local
# benchmarks raise them (``Limits``).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_LOGO_BYTES = 500 * 1024
MAX_FIELD_BYTES = 1024
MAX_PART_HEADER_BYTES = 4 * 1024
MAX_PARTS = 10
_BODY_OVERHEAD_BYTES = 64 * 1024  # boundaries, part headers and text fields
# Both files at their caps, plus room for boundaries, part headers and text fields.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + MAX_LOGO_BYTES + _BODY_OVERHEAD_BYTES
MAX_COMPANY_CHARS = 100
MAX_FOOTER_CHARS = 200
DEFAULT_PRIMARY_HEX = "#000000"

DATA_FIELD = "file"
LOGO_FIELD = "logo"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
MAX_LOGO_SIDE_PX = 2000  # width and height, read from the image header before decoding

# PDF generation is CPU- and memory-heavy: at most ``Settings.generate_slots`` /generate
# jobs (GENERATE_SLOTS, default 1) run at once per process. Further requests queue for up
# to ``Settings.generate_wait_seconds`` (GENERATE_WAIT_SECONDS, default 60), then get a 503.
BUSY_RETRY_AFTER_SECONDS = 60
QUOTA_STATUS = 429  # Too Many Requests: the organization's daily row quota is used up

MAX_ISSUES_SHOWN = 200  # rows of the issue table; the summary always counts them all
_PART_NUMBER = re.compile(r"[0-9]{1,4}")  # ASCII digits only
_BATCH_ID = re.compile(r"[0-9]{1,19}")  # ASCII digits only
MAX_BATCH_ID = 2**63 - 1  # bigint

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
# The browser's local date and time as DDMMYYYY_HHMM, used in the ZIP filename.
# ASCII digits only: the value goes into a response header.
_CLIENT_STAMP = re.compile(r"[0-9]{8}_[0-9]{4}")
STAMP_FORMAT = "%d%m%Y_%H%M"

DEFAULT_LOGO_PATH: Path = Path(__file__).parent / "static" / "brand" / "logo.png"

MSG_MALFORMED = "The upload could not be read. Reload the page and try again."
MSG_NO_FILE = "Choose a .csv or .xlsx file to upload."
MSG_UNSUPPORTED = "Unsupported file type. Upload a .csv or .xlsx file."


def _body_too_large_message(upload_bytes: int) -> str:
    return (
        f"The upload is too large. Data files can be up to {upload_bytes // (1024 * 1024)} MB "
        f"and logos up to {MAX_LOGO_BYTES // 1024} KB."
    )


def _too_large_messages(upload_bytes: int) -> dict[str, str]:
    return {
        DATA_FIELD: (
            f"The data file is larger than {upload_bytes // (1024 * 1024)} MB. "
            "Split it into smaller files and upload them one at a time."
        ),
        LOGO_FIELD: f"The logo is larger than {MAX_LOGO_BYTES // 1024} KB. Use a smaller image.",
    }


MSG_BODY_TOO_LARGE = _body_too_large_message(MAX_UPLOAD_BYTES)
MSG_TOO_LARGE: dict[str, str] = _too_large_messages(MAX_UPLOAD_BYTES)
MSG_FIELD_TOO_LARGE = "A form field is too long."
MSG_NO_ROWS = "The file has a header row but no data rows."
MSG_BAD_LOGO = "The logo must be a PNG or JPEG image."
MSG_LOGO_DIMENSIONS = (
    f"The logo can be at most {MAX_LOGO_SIDE_PX} x {MAX_LOGO_SIDE_PX} pixels. Use a smaller image."
)
MSG_BUSY = "Server busy, try again in a minute."
MSG_BAD_PART = (
    "The requested part does not match this file. Choose the file again and restart the download."
)
MSG_COMPANY_MISSING = "Enter a company name."
MSG_COMPANY_LONG = f"The company name can be at most {MAX_COMPANY_CHARS} characters."
MSG_FOOTER_LONG = f"The footer text can be at most {MAX_FOOTER_CHARS} characters."
MSG_BAD_COLOUR = "The primary colour must be a hex colour such as #1A4D8F."
MSG_CONTROL_CHARS = "Company name and footer text must not contain control characters."
MSG_NO_ORGANIZATION = "Generating PDFs needs an account in an organization."
MSG_NO_BATCH = "The download is missing its file check. Choose the file again."
MSG_BATCH_GONE = "This upload is no longer available. Choose the file again."
# Shown only without app.js, which sends the part again with the file on a 428.
MSG_UPLOAD_NEEDED = "The server needs the file again for this part. Choose the file again."
UPLOAD_NEEDED_HEADER = "X-Esim-Upload-Needed"
UPLOAD_NEEDED_STATUS = 428  # Precondition Required: send the part again with the file

# Every route here needs a logged-in user, and every POST the session's CSRF token.
router = APIRouter(dependencies=[Depends(require_user)])


@dataclass(frozen=True, slots=True)
class Limits:
    """Upload limits: the product values unless settings raise them (benchmarks)."""

    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_rows: int = MAX_ROWS

    @property
    def max_body_bytes(self) -> int:
        return self.max_upload_bytes + MAX_LOGO_BYTES + _BODY_OVERHEAD_BYTES

    @property
    def part_limits(self) -> dict[str, int]:
        return {DATA_FIELD: self.max_upload_bytes, LOGO_FIELD: MAX_LOGO_BYTES}

    @property
    def too_large(self) -> dict[str, str]:
        if self.max_upload_bytes == MAX_UPLOAD_BYTES:
            return MSG_TOO_LARGE
        return _too_large_messages(self.max_upload_bytes)

    @property
    def body_too_large(self) -> str:
        if self.max_upload_bytes == MAX_UPLOAD_BYTES:
            return MSG_BODY_TOO_LARGE
        return _body_too_large_message(self.max_upload_bytes)


DEFAULT_LIMITS = Limits()


def limits_of(request: Request) -> Limits:
    """The app's limits, from its settings (``MAX_UPLOAD_MB``, ``MAX_ROWS``)."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return DEFAULT_LIMITS
    return Limits(max_upload_bytes=settings.max_upload_bytes, max_rows=settings.max_rows)


def upload_cache_of(request: Request) -> UploadCache:
    return request.app.state.upload_cache


class UploadRejected(Exception):
    """The request cannot be processed. ``message`` is safe to show to the user."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """One file part, held in memory only."""

    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class UploadForm:
    """A parsed multipart form: text fields and file parts, by field name."""

    fields: dict[str, str]
    files: dict[str, UploadedFile]


@dataclass(frozen=True, slots=True)
class Results:
    """What the results partial shows: row count, number of parts, validation report,
    the rows used by earlier batches (if any) and, for a file that passed, its batch id.
    ``continuation`` is set when the file continues an unfinished batch (its id is
    ``batch_id`` then)."""

    row_count: int
    report: ValidationReport
    used: UsedSummary | None = None
    batch_id: int | None = None
    continuation: Continuation | None = None

    @property
    def chunk_rows(self) -> int:
        """Rows per part: the continued batch's own size, else ``GENERATE_CHUNK_ROWS``."""
        if self.continuation is not None:
            return self.continuation.chunk_rows
        return pipeline.GENERATE_CHUNK_ROWS

    @property
    def parts(self) -> int:
        """How many ZIP files (parts of ``chunk_rows`` rows) the upload needs."""
        return pipeline.part_count(self.row_count, self.chunk_rows)


@lru_cache(maxsize=1)
def _default_logo() -> bytes:
    return DEFAULT_LOGO_PATH.read_bytes()


def get_branding() -> Branding:
    """Dependency that supplies the default branding (placeholder logo plus settings).

    The defaults pre-fill the form, and the logo is used when none is uploaded.
    Tests can override it.
    """
    return Branding(
        company_name="",
        logo_png=_default_logo(),
        primary_hex=DEFAULT_PRIMARY_HEX,
        footer_text="",
    )


# --- Reading the upload --------------------------------------------------------


class _FormCollector:
    """``MultipartParser`` callbacks that keep each part in memory under its size cap."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        self.fields: dict[str, str] = {}
        self.files: dict[str, UploadedFile] = {}
        self._parts = 0
        self._part_limits = limits.part_limits
        self._too_large = limits.too_large
        self._reset_part()

    def _reset_part(self) -> None:
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._header_bytes = 0
        self._disposition = b""
        self._name = ""
        self._filename: str | None = None
        self._limit = MAX_FIELD_BYTES
        self._data = bytearray()

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise UploadRejected(400, MSG_MALFORMED)
        self._reset_part()

    def _add_header_bytes(self, target: bytearray, chunk: bytes) -> None:
        self._header_bytes += len(chunk)
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            raise UploadRejected(400, MSG_MALFORMED)
        target += chunk

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._add_header_bytes(self._header_name, data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._add_header_bytes(self._header_value, data[start:end])

    def on_header_end(self) -> None:
        if bytes(self._header_name).strip().lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        raw_name = options.get(b"name")
        if raw_name is None:
            raise UploadRejected(400, MSG_MALFORMED)
        self._name = raw_name.decode("utf-8", errors="replace")
        if self._name in self.fields or self._name in self.files:
            raise UploadRejected(400, MSG_MALFORMED)
        raw_filename = options.get(b"filename")
        if raw_filename is None:
            return  # a text field: MAX_FIELD_BYTES
        self._filename = raw_filename.decode("utf-8", errors="replace")
        self._limit = self._part_limits.get(self._name, MAX_FIELD_BYTES)
        if self._name == DATA_FIELD and self._filename:
            _check_extension(self._filename)  # before reading any of the file

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if len(self._data) + (end - start) > self._limit:
            raise UploadRejected(413, self._too_large.get(self._name, MSG_FIELD_TOO_LARGE))
        self._data += data[start:end]

    def on_part_end(self) -> None:
        if self._filename is None:
            try:
                self.fields[self._name] = self._data.decode("utf-8")
            except UnicodeDecodeError:
                raise UploadRejected(400, MSG_MALFORMED) from None
        else:
            self.files[self._name] = UploadedFile(self._filename, bytes(self._data))
        self._data = bytearray()


def _check_extension(filename: str) -> None:
    if os.path.splitext(filename)[1].lower() not in SUPPORTED_EXTENSIONS:
        raise UploadRejected(400, MSG_UNSUPPORTED)


async def read_capped(request: Request, limits: Limits | None = None) -> UploadForm:
    """Stream the multipart body into memory, enforcing the size caps as bytes arrive.

    Raises ``UploadRejected`` with 413 as soon as the data file passes
    ``max_upload_bytes`` (``MAX_UPLOAD_BYTES`` unless settings raise it), the logo
    passes ``MAX_LOGO_BYTES``, a text field passes ``MAX_FIELD_BYTES`` or the body
    passes ``max_body_bytes``. The rest of the body is not read. A declared
    Content-Length above ``max_body_bytes`` is refused before anything is read, and so
    is a data file with an unsupported extension (400). Nothing is written to disk.
    """
    limits = limits if limits is not None else limits_of(request)
    max_body = limits.max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise UploadRejected(400, MSG_MALFORMED)
        if int(declared) > max_body:
            raise UploadRejected(413, limits.body_too_large)

    content_type, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadRejected(400, MSG_MALFORMED)

    collector = _FormCollector(limits)
    received = 0
    try:
        parser = MultipartParser(boundary, collector.callbacks())  # type: ignore[arg-type]
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body:
                raise UploadRejected(413, limits.body_too_large)
            parser.write(chunk)
        parser.finalize()
    except (FormParserError, ValueError):
        raise UploadRejected(400, MSG_MALFORMED) from None
    return UploadForm(collector.fields, collector.files)


def _data_file(form: UploadForm) -> UploadedFile:
    upload = form.files.get(DATA_FIELD)
    if upload is None or not upload.filename:
        raise UploadRejected(400, MSG_NO_FILE)
    return upload


def _optional_data_file(form: UploadForm) -> UploadedFile | None:
    """/generate: no ``file`` field at all means "use the cached rows" (app.js leaves it
    out). A ``file`` field without a file chosen (empty or no filename, so possibly
    parsed as a text field) is refused as on /validate."""
    if DATA_FIELD not in form.files and DATA_FIELD not in form.fields:
        return None
    return _data_file(form)


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _branding(form: UploadForm, defaults: Branding) -> Branding:
    """Build the batch branding from the form; raises ``UploadRejected`` (400)."""
    company = form.fields.get("company_name", "").strip()
    footer = form.fields.get("footer_text", "").strip()
    colour = form.fields.get("primary_color", "").strip() or defaults.primary_hex
    if not company:
        raise UploadRejected(400, MSG_COMPANY_MISSING)
    if len(company) > MAX_COMPANY_CHARS:
        raise UploadRejected(400, MSG_COMPANY_LONG)
    if len(footer) > MAX_FOOTER_CHARS:
        raise UploadRejected(400, MSG_FOOTER_LONG)
    if _has_control_chars(company) or _has_control_chars(footer):
        raise UploadRejected(400, MSG_CONTROL_CHARS)
    if not _HEX_COLOUR.fullmatch(colour):
        raise UploadRejected(400, MSG_BAD_COLOUR)

    logo_png = defaults.logo_png
    logo = form.files.get(LOGO_FIELD)
    if logo is not None and logo.filename:  # an empty filename means no file was chosen
        size = logo_dimensions(logo.data)  # by content, never by extension
        if size is None:
            raise UploadRejected(400, MSG_BAD_LOGO)
        if max(size) > MAX_LOGO_SIDE_PX:
            raise UploadRejected(400, MSG_LOGO_DIMENSIONS)
        logo_png = logo.data
    return Branding(company_name=company, logo_png=logo_png, primary_hex=colour, footer_text=footer)


# JPEG start-of-frame markers carry the image size (C4, C8 and CC are not frames).
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_NO_LENGTH_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})  # TEM, RST0-7
_JPEG_END_OF_HEADERS = frozenset({0xD9, 0xDA})  # EOI, SOS: image data follows


def logo_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` read from a PNG or JPEG header, without decoding pixels.

    Returns ``None`` if ``data`` is not a PNG or JPEG (by signature) or its header
    is unreadable. For JPEG every frame header before the image data is read and
    the largest width and height are returned.
    """
    if data.startswith(PNG_SIGNATURE):
        return _png_dimensions(data)
    if data.startswith(JPEG_SIGNATURE):
        return _jpeg_dimensions(data)
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # The first chunk must be IHDR: length (4), type (4), width (4), height (4).
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    width = height = 0
    pos = 2  # after SOI
    while pos < len(data):
        if data[pos] != 0xFF:
            return None  # every header segment starts with a marker
        while pos < len(data) and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker in _JPEG_NO_LENGTH_MARKERS:
            continue
        if marker in _JPEG_END_OF_HEADERS:
            break
        if pos + 2 > len(data):
            return None
        (length,) = struct.unpack(">H", data[pos : pos + 2])
        if length < 2 or pos + length > len(data):
            return None  # every header segment must be complete
        if marker in _JPEG_SOF_MARKERS:
            # Segment: length (2), precision (1), height (2), width (2), ...
            if length < 7:
                return None
            frame_height, frame_width = struct.unpack(">HH", data[pos + 3 : pos + 7])
            if not frame_height or not frame_width:
                return None
            width, height = max(width, frame_width), max(height, frame_height)
        pos += length
    return (width, height) if width and height else None


def _batch_stamp(form: UploadForm) -> str:
    """The client's local ``DDMMYYYY_HHMM`` stamp if well-formed, else the current UTC time."""
    stamp = form.fields.get("client_stamp", "")
    if _CLIENT_STAMP.fullmatch(stamp):
        return stamp
    return datetime.now(UTC).strftime(STAMP_FORMAT)


def _batch_id(form: UploadForm) -> int:
    """The ``batch_id`` field. Raises ``UploadRejected`` (400) when missing or malformed."""
    value = form.fields.get("batch_id", "")
    if not _BATCH_ID.fullmatch(value) or not 1 <= int(value) <= MAX_BATCH_ID:
        raise UploadRejected(400, MSG_NO_BATCH)
    return int(value)


def _part_number(form: UploadForm, field: str) -> int:
    """A part field as an integer; 1 when absent. Raises ``UploadRejected`` (400)."""
    value = form.fields.get(field)
    if value is None:
        return 1
    if not _PART_NUMBER.fullmatch(value):
        raise UploadRejected(400, MSG_BAD_PART)
    return int(value)


def _zip_filename(stamp: str, part: int, parts: int) -> str:
    """``esim_batch_<stamp>.zip``, with ``_partNNofMM`` when there is more than one part."""
    if parts == 1:
        return f"esim_batch_{stamp}.zip"
    width = max(2, len(str(parts)))
    return f"esim_batch_{stamp}_part{part:0{width}d}of{parts:0{width}d}.zip"


def _fingerprint_key(request: Request) -> bytes:
    key = request.app.state.settings.fingerprint_key
    if key is None:  # create_app refuses to start without it
        raise RuntimeError("FINGERPRINT_KEY is not set")
    return key.reveal().encode("utf-8")


class QuotaRefused(Exception):
    """The organization's daily row quota is used up. ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _over_quota(
    db: Session,
    organization_id: int,
    rows: int,
    now: datetime,
    *,
    user_id: int | None,
    ip: IpPrefix | None,
    batch_id: int | None = None,
) -> DailyUsage | None:
    """``None`` if ``rows`` more fit in today's quota; else today's usage, after adding the
    ``quota_exceeded`` audit row to the transaction (which the caller commits)."""
    try:
        batches.check_quota(db, organization_id, rows, now=now)
    except batches.QuotaExceeded as exc:
        batches.audit_quota_exceeded(
            db,
            now=now,
            user_id=user_id,
            organization_id=organization_id,
            ip=ip,
            batch_id=batch_id,
        )
        return exc.usage
    return None


def _check(
    sessions: sessionmaker[Session],
    key: bytes,
    user: CurrentUser,
    organization_id: int,
    upload: UploadedFile,
    now: datetime,
    *,
    cache: UploadCache | None = None,
    max_rows: int | None = None,
    ip: IpPrefix | None = None,
) -> Results:
    """Parse, validate, check against earlier batches; a file that passes gets a batch.

    A file whose only duplicates are an unfinished batch's own rows, in the same parts,
    continues that batch (``batches.continuable``): no new batch, the old id is reused.
    Either way the checked rows go into ``cache`` under the batch id, for /generate.

    A file that passes is refused when its rows still to generate (all of them for a new
    batch; the parts not downloaded yet for a continued one) would take today's usage over
    the quota: no batch, nothing cached, a ``quota_exceeded`` audit row, and
    ``QuotaRefused``. Raises ``ParseError``.
    """
    parsed, report = check_upload(upload.data, upload.filename, max_rows=max_rows)
    if not parsed.records:
        raise ParseError(MSG_NO_ROWS)
    row_count = len(parsed.records)
    chunk_rows = pipeline.GENERATE_CHUNK_ROWS  # a new batch keeps today's part size
    refused: DailyUsage | None = None
    results: Results | None = None
    with sessions.begin() as db:
        used = batches.find_used(db, organization_id, report.valid, key=key, now=now)
        continuation = batches.continuable(db, organization_id, report, used, row_count=row_count)
        if continuation is not None:
            to_generate = batches.remaining_rows(
                row_count, continuation.chunk_rows, continuation.parts, continuation.downloaded
            )
            refused = _over_quota(
                db,
                organization_id,
                to_generate,
                now,
                user_id=user.user_id,
                ip=ip,
                batch_id=continuation.batch_id,
            )
            if refused is None:
                results = Results(
                    row_count=row_count,
                    report=report,
                    batch_id=continuation.batch_id,
                    continuation=continuation,
                )
                chunk_rows = continuation.chunk_rows
        else:
            report = batches.merge(report, used)
            batch_id = None
            if report.ok:
                refused = _over_quota(
                    db, organization_id, row_count, now, user_id=user.user_id, ip=ip
                )
            if report.ok and refused is None:
                batch_id = batches.create_batch(
                    db,
                    organization_id=organization_id,
                    user_id=user.user_id,
                    file_name=upload.filename,
                    row_count=row_count,
                    parts=pipeline.part_count(row_count, chunk_rows),
                    warning_count=len(report.warnings),
                    now=now,
                    chunk_rows=chunk_rows,
                )
            results = Results(
                row_count=row_count, report=report, used=used.summary, batch_id=batch_id
            )
    if refused is not None:  # the audit row is committed
        raise QuotaRefused(batches.quota_message(refused, now))
    if results is None:  # not reached: set whenever nothing was refused
        raise RuntimeError("no results")
    if cache is not None and results.batch_id is not None:  # committed: cache the rows
        _remember(cache, organization_id, results.batch_id, chunk_rows, parsed, report)
    return results


def _remember(
    cache: UploadCache,
    organization_id: int,
    batch_id: int,
    chunk_rows: int,
    parsed: ParsedUpload,
    report: ValidationReport,
) -> None:
    """Cache a batch's checked rows. Only a file whose every row is valid has a batch."""
    if len(report.valid) != len(parsed.records):
        return
    cache.put(
        CachedUpload.build(
            organization_id=organization_id,
            batch_id=batch_id,
            chunk_rows=chunk_rows,
            has_email=parsed.has_email,
            records=report.valid,
        )
    )


class BatchGone(Exception):
    """The batch does not exist or belongs to another organization."""


class UploadNeeded(Exception):
    """/generate without the file, and the batch's rows are not cached (any more)."""


MSG_BATCH_VOIDED = "This batch was voided. Choose the file again."


@dataclass(frozen=True, slots=True)
class PartOutcome:
    """A generated part (``archive``), or the results to show instead (``archive is None``)."""

    results: Results
    archive: bytes | None


@dataclass(frozen=True, slots=True, repr=False)
class _PartRows:
    """What rendering one part needs: its rows, and a report for error pages."""

    row_count: int  # the whole file's
    records: list[EsimRecord]  # this part's rows
    has_email: bool
    report: ValidationReport

    def __repr__(self) -> str:
        return f"_PartRows(row_count={self.row_count}, part_rows={len(self.records)})"


def _generate_part(
    sessions: sessionmaker[Session],
    key: bytes,
    organization_id: int,
    batch_id: int,
    upload: UploadedFile | None,
    branding: Branding,
    part: int,
    parts: int,
    now: datetime,
    *,
    cache: UploadCache | None = None,
    max_rows: int | None = None,
    user_id: int | None = None,
    ip: IpPrefix | None = None,
) -> PartOutcome:
    """Render one part of the batch and record it. Errors mark the batch ``failed``.

    Without ``upload``, the part's rows come from ``cache`` (no parse, no whole-file
    check); ``UploadNeeded`` is raised when they are not cached. With ``upload``, the
    whole file is validated again (including earlier batches) and its rows are cached.
    Either way ``batches.record_part`` checks the part's rows against other batches
    again, under the organization's lock. The cached rows are dropped when the batch is
    completed or fails.

    Every part uses the batch's own part size (``Batch.chunk_rows``), so a batch made
    before ``GENERATE_CHUNK_ROWS`` changed keeps its parts.

    Raises ``BatchGone`` (checked first, so another organization's batch id never
    reaches the cache), ``batches.BatchVoided``, ``UploadNeeded``, ``PartError`` (also
    when the file or cached rows do not match the batch), ``ParseError``, and
    ``QuotaRefused`` when ``record_part`` finds the daily quota used up meanwhile (nothing
    is recorded, a ``quota_exceeded`` audit row is written, the cached rows are kept). No
    database connection is held while PDFs render.
    """
    with sessions.begin() as db:
        batch = batches.scoped_batch(db, organization_id, batch_id)
        if batch is None:
            raise BatchGone
        if batch.status == BatchStatus.VOIDED:
            raise batches.BatchVoided
        expected = (batch.row_count, batch.parts)
        chunk_rows = batch.chunk_rows
    if not 1 <= part <= parts:
        raise PartError(f"Part {part} of {parts} does not exist.")

    if upload is None:
        cached = cache.get(organization_id, batch_id) if cache is not None else None
        if cached is None:
            raise UploadNeeded
        rows = _cached_part(cached, part, parts, chunk_rows, expected)
    else:
        checked = _checked_part(
            sessions,
            key,
            organization_id,
            batch_id,
            upload,
            part,
            parts,
            now,
            chunk_rows=chunk_rows,
            expected=expected,
            cache=cache,
            max_rows=max_rows,
        )
        if isinstance(checked, PartOutcome):
            return checked
        rows = checked

    archive = pipeline.build_zip_for(rows.records, branding, has_email=rows.has_email)
    completed = False
    try:
        with sessions.begin() as db:
            race = batches.record_part(  # raises BatchVoided if voided meanwhile
                db, organization_id, batch_id, part, rows.records, key=key, now=now
            )
            if race.found:  # another batch generated the same codes meanwhile
                report = batches.merge(rows.report, race)
                batches.mark_failed(db, organization_id, batch_id, report)
            else:
                recorded = db.get(Batch, batch_id)  # loaded by record_part: no query
                completed = recorded is not None and recorded.status == BatchStatus.COMPLETED
    except batches.QuotaExceeded as exc:  # another part used up the quota meanwhile
        with sessions.begin() as db:
            batches.audit_quota_exceeded(
                db,
                now=now,
                user_id=user_id,
                organization_id=organization_id,
                ip=ip,
                batch_id=batch_id,
            )
        raise QuotaRefused(batches.quota_message(exc.usage, now)) from None
    if cache is not None and (race.found or completed):
        cache.discard(batch_id)
    if race.found:
        return PartOutcome(Results(rows.row_count, report, race.summary), None)
    return PartOutcome(Results(rows.row_count, rows.report), archive)


def _check_part_quota(
    sessions: sessionmaker[Session],
    organization_id: int,
    batch_id: int,
    part: int,
    now: datetime,
    *,
    user_id: int | None,
    ip: IpPrefix | None,
) -> None:
    """Before /generate waits for a slot: refuse a part not generated yet whose rows
    (by the batch's own part size) would take today's usage over the quota.

    Raises ``QuotaRefused`` (after committing the ``quota_exceeded`` audit row). Batches
    that are not the organization's, voided ones and parts that do not exist pass here
    and are answered by ``_generate_part``. ``record_part`` checks again under the lock.
    """
    refused: DailyUsage | None = None
    with sessions.begin() as db:
        batch = batches.scoped_batch(db, organization_id, batch_id)
        if batch is None or batch.status == BatchStatus.VOIDED:
            return
        rows = batches.part_rows(batch.row_count, part, batch.chunk_rows)
        if rows <= 0 or part > batch.parts or part in batches.downloaded_parts(db, batch_id):
            return
        refused = _over_quota(
            db, organization_id, rows, now, user_id=user_id, ip=ip, batch_id=batch_id
        )
    if refused is not None:
        raise QuotaRefused(batches.quota_message(refused, now))


def _cached_part(
    cached: CachedUpload, part: int, parts: int, chunk_rows: int, expected: tuple[int, int]
) -> _PartRows:
    """The part's rows from the cache; ``PartError`` unless they match the batch."""
    if (
        (cached.row_count, parts) != expected
        or cached.chunk_rows != chunk_rows
        or pipeline.part_count(cached.row_count, chunk_rows) != parts
    ):
        raise PartError("The cached rows do not match their batch.")
    records = pipeline.part_slice(cached.records, part, chunk_rows)
    # The file passed when it was cached; there is no whole-file report. If another
    # batch took one of these codes meanwhile, the error page lists this part's rows.
    return _PartRows(cached.row_count, records, cached.has_email, ValidationReport(records))


def _checked_part(
    sessions: sessionmaker[Session],
    key: bytes,
    organization_id: int,
    batch_id: int,
    upload: UploadedFile,
    part: int,
    parts: int,
    now: datetime,
    *,
    chunk_rows: int,
    expected: tuple[int, int],
    cache: UploadCache | None,
    max_rows: int | None,
) -> _PartRows | PartOutcome:
    """Validate the whole re-sent file (including earlier batches) and cache its rows.

    Returns the part's rows, or the outcome to show instead (errors mark the batch
    failed and drop its cached rows).
    """
    parsed, report = check_upload(upload.data, upload.filename, max_rows=max_rows)
    pipeline.check_part(parsed, part, parts, chunk_rows)
    row_count = len(parsed.records)
    if (row_count, parts) != expected:
        raise PartError("The upload does not match its batch.")

    used = batches.NOTHING_USED
    if report.ok:
        with sessions.begin() as db:
            used = batches.find_used(
                db, organization_id, report.valid, key=key, now=now, exclude_batch_id=batch_id
            )
        report = batches.merge(report, used)
    if not report.ok:
        with sessions.begin() as db:
            batches.mark_failed(db, organization_id, batch_id, report)
        if cache is not None:
            cache.discard(batch_id)
        return PartOutcome(Results(row_count, report, used.summary), None)
    if not report.valid:
        return PartOutcome(Results(row_count, report), None)
    if cache is not None:
        _remember(cache, organization_id, batch_id, chunk_rows, parsed, report)
    records = pipeline.part_records(report, part, chunk_rows)
    return _PartRows(row_count, records, parsed.has_email, report)


# --- Responses -----------------------------------------------------------------


def _form_values(defaults: Branding, form: UploadForm | None = None) -> dict[str, str]:
    """Text values to pre-fill the form with: the submitted ones, else the defaults."""
    fields = form.fields if form is not None else {}
    return {
        "company_name": fields.get("company_name", defaults.company_name),
        "primary_color": fields.get("primary_color", defaults.primary_hex),
        "footer_text": fields.get("footer_text", defaults.footer_text),
    }


def _context(limits: Limits = DEFAULT_LIMITS, **extra: Any) -> dict[str, Any]:
    return {
        "max_upload_bytes": limits.max_upload_bytes,
        "max_upload_mb": limits.max_upload_bytes // (1024 * 1024),
        "max_logo_bytes": MAX_LOGO_BYTES,
        "max_logo_kb": MAX_LOGO_BYTES // 1024,
        "max_rows": limits.max_rows,
        "chunk_rows": pipeline.GENERATE_CHUNK_ROWS,
        "max_issues_shown": MAX_ISSUES_SHOWN,
        "max_company_chars": MAX_COMPANY_CHARS,
        "max_footer_chars": MAX_FOOTER_CHARS,
        "error": None,
        "results": None,
        **extra,
    }


def _rejection_headers(status_code: int) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if status_code == 413:
        # Part of the body is still unread, so the connection cannot be reused.
        headers["Connection"] = "close"
    elif status_code == 503:
        headers["Retry-After"] = str(BUSY_RETRY_AFTER_SECONDS)
    elif status_code == UPLOAD_NEEDED_STATUS:
        headers[UPLOAD_NEEDED_HEADER] = "1"
    return headers


def generate_slot_count(request: Request) -> int:
    """``Settings.generate_slots`` (GENERATE_SLOTS): /generate jobs at once per process."""
    settings = getattr(request.app.state, "settings", None)
    return settings.generate_slots if settings is not None else DEFAULT_GENERATE_SLOTS


def generate_wait_seconds(request: Request) -> float:
    """``Settings.generate_wait_seconds`` (GENERATE_WAIT_SECONDS): how long a /generate
    request queues for a free job slot before it gets 503."""
    settings = getattr(request.app.state, "settings", None)
    return settings.generate_wait_seconds if settings is not None else DEFAULT_GENERATE_WAIT_SECONDS


def _generate_slots(request: Request) -> asyncio.Semaphore:
    """The app's job slots on the running loop.

    One semaphore per app and event loop: an asyncio.Semaphore is bound to the loop it
    first waits on. Uvicorn runs one loop per worker process, so in production this is
    one process-wide limit; tests that start their own loops get fresh semaphores.
    """
    state = request.app.state
    per_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] | None
    per_loop = getattr(state, "generate_slots", None)
    if per_loop is None:
        per_loop = state.generate_slots = weakref.WeakKeyDictionary()
    loop = asyncio.get_running_loop()
    slots = per_loop.get(loop)
    if slots is None:
        slots = per_loop[loop] = asyncio.Semaphore(generate_slot_count(request))
    return slots


def today_usage(sessions: sessionmaker[Session], organization_id: int) -> DailyUsage:
    """The organization's rows generated today and its quota ("Today: X / Y rows")."""
    with sessions.begin() as db:
        return batches.daily_usage(db, organization_id, now=utc_now())


def _page(
    request: Request, form_values: dict[str, str], status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        _context(limits_of(request), form=form_values, **extra),
        status_code=status_code,
        headers=_rejection_headers(status_code),
    )


def _partial(request: Request, status_code: int = 200, **extra: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_results.html",
        _context(limits_of(request), **extra),
        status_code=status_code,
        headers=_rejection_headers(status_code),
    )


# --- Routes --------------------------------------------------------------------


User = Annotated[CurrentUser, Depends(require_user)]


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, defaults: Annotated[Branding, Depends(get_branding)], user: User
) -> HTMLResponse:
    """GET /: render the upload form (a message instead for users without an organization)."""
    if user.organization_id is None:
        return _page(request, _form_values(defaults), no_organization=MSG_NO_ORGANIZATION)
    today = await run_in_threadpool(today_usage, sessions_of(request), user.organization_id)
    return _page(request, _form_values(defaults), today_usage=today)


TEMPLATE_FILENAME = "esim_template.xlsx"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@lru_cache(maxsize=1)
def _template_xlsx() -> bytes:
    return sheets.build_template()  # deterministic, so built once per process


@router.get("/template.xlsx", response_model=None)
async def template_xlsx() -> Response:
    """GET /template.xlsx: the upload template (all columns as text, one synthetic row)."""
    return Response(
        content=_template_xlsx(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


@router.post("/validate", response_class=HTMLResponse)
async def validate_upload(request: Request, user: User) -> HTMLResponse:
    """POST /validate (HTMX): parse, validate, check against earlier batches; return the
    ``_results.html`` partial. A file that passes gets a batch, whose id the partial puts
    into the form for /generate, and its checked rows are kept in the upload cache."""
    if user.organization_id is None:
        return _partial(request, 403, error=MSG_NO_ORGANIZATION)
    try:
        upload = _data_file(await read_capped(request))
    except UploadRejected as exc:
        return _partial(request, exc.status_code, error=exc.message)
    try:
        results = await run_in_threadpool(
            _check,
            sessions_of(request),
            _fingerprint_key(request),
            user,
            user.organization_id,
            upload,
            utc_now(),
            cache=upload_cache_of(request),
            max_rows=limits_of(request).max_rows,
            ip=client_ip_prefix(request),
        )
    except ParseError as exc:
        return _partial(request, error=str(exc))
    except QuotaRefused as exc:
        return _partial(request, QUOTA_STATUS, error=exc.message)
    return _partial(request, results=results)


@router.post("/generate", response_model=None)
async def generate(
    request: Request, defaults: Annotated[Branding, Depends(get_branding)], user: User
) -> Response:
    """POST /generate: build the ZIP for one part of a batch.

    The form field ``batch_id`` (from /validate) is required. The fields ``part``
    (1-based) and ``parts`` say which part; both default to 1, which is right for
    uploads of up to one part. The page's script requests the parts one after another.

    The ``file`` part is optional. Without it the part is built from the rows /validate
    cached; when they are not cached the answer is 428 with ``X-Esim-Upload-Needed: 1``
    and the page sends the part again with the file. With it the whole file is
    validated again (as before the cache) and cached.

    Returns the ZIP when every row is valid and none was generated by another batch.
    Otherwise: the full page with the results table (422; the batch is marked failed),
    the reason the request was rejected (400/413, including a missing batch id and a
    part or file that does not match the batch), 404 for a batch that is not the
    organization's or no longer exists, 428 (see above), 429 when the part (not generated
    before) would exceed the organization's daily row quota, or a busy message (503,
    ``Retry-After``) when no job slot frees up within ``generate_wait_seconds`` (the
    request queues behind the running parts until then).

    The pipeline runs in a worker thread. The slot is held until that thread
    finishes, even if the client disconnects: the thread call is not abandoned
    on cancellation, so at most ``GENERATE_SLOTS`` pipelines ever run.
    """
    form_values = _form_values(defaults)
    try:
        form = await read_capped(request)
        form_values = _form_values(defaults, form)
        upload = _optional_data_file(form)
        batch_id = _batch_id(form)
        part, parts = _part_number(form, "part"), _part_number(form, "parts")
        branding = _branding(form, defaults)
    except UploadRejected as exc:
        return _page(request, form_values, exc.status_code, error=exc.message)
    organization_id = user.organization_id
    if organization_id is None:
        return _page(request, form_values, 404, error=MSG_BATCH_GONE)

    sessions = sessions_of(request)
    ip = client_ip_prefix(request)
    try:  # before waiting for a slot: a refused part neither queues nor renders
        await run_in_threadpool(
            _check_part_quota,
            sessions,
            organization_id,
            batch_id,
            part,
            utc_now(),
            user_id=user.user_id,
            ip=ip,
        )
    except QuotaRefused as exc:
        return _page(request, form_values, QUOTA_STATUS, error=exc.message)

    slots = _generate_slots(request)
    try:
        await asyncio.wait_for(slots.acquire(), generate_wait_seconds(request))
    except TimeoutError:
        return _page(request, form_values, 503, error=MSG_BUSY)
    try:
        outcome = await run_in_threadpool(
            _generate_part,
            sessions,
            _fingerprint_key(request),
            organization_id,
            batch_id,
            upload,
            branding,
            part,
            parts,
            utc_now(),
            cache=upload_cache_of(request),
            max_rows=limits_of(request).max_rows,
            user_id=user.user_id,
            ip=ip,
        )
    except QuotaRefused as exc:
        return _page(request, form_values, QUOTA_STATUS, error=exc.message)
    except BatchGone:
        return _page(request, form_values, 404, error=MSG_BATCH_GONE)
    except batches.BatchVoided:  # the user's own organization's batch: no 404 needed
        return _page(request, form_values, 409, error=MSG_BATCH_VOIDED)
    except UploadNeeded:
        return _page(request, form_values, UPLOAD_NEEDED_STATUS, error=MSG_UPLOAD_NEEDED)
    except ParseError as exc:
        return _page(request, form_values, 422, error=str(exc))
    except PartError:
        return _page(request, form_values, 400, error=MSG_BAD_PART)
    finally:
        slots.release()
    archive = outcome.archive
    if archive is None:
        if outcome.results.report.ok:  # no errors, but no valid rows either
            return _page(request, form_values, 422, error=MSG_NO_ROWS)
        return _page(request, form_values, 422, results=outcome.results)

    filename = _zip_filename(_batch_stamp(form), part, parts)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

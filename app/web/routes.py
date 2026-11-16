"""HTTP routes: the upload form, HTMX validation, and ZIP generation.

Stateless: uploads are read from the request stream straight into memory with
per-part caps (``read_capped``) and dropped when the response is sent. Uploads
never go to disk. Starlette's own form parser is not used because it spools
file parts over 1 MB to a temporary file.

Large orders are generated in parts of ``GENERATE_CHUNK_ROWS`` rows. The page
asks for one part at a time and sends the whole file with every request; the
server keeps nothing between requests.

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
from fastapi.templating import Jinja2Templates
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from app.core import pipeline
from app.core.models import Branding, ParseError, ValidationReport
from app.core.parser import MAX_ROWS, SUPPORTED_EXTENSIONS
from app.core.pipeline import PartError, check_upload, process_upload

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_LOGO_BYTES = 500 * 1024
MAX_FIELD_BYTES = 1024
MAX_PART_HEADER_BYTES = 4 * 1024
MAX_PARTS = 10
# Both files at their caps, plus room for boundaries, part headers and text fields.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + MAX_LOGO_BYTES + 64 * 1024
MAX_COMPANY_CHARS = 100
MAX_FOOTER_CHARS = 200
DEFAULT_PRIMARY_HEX = "#000000"

DATA_FIELD = "file"
LOGO_FIELD = "logo"
PART_LIMITS: dict[str, int] = {DATA_FIELD: MAX_UPLOAD_BYTES, LOGO_FIELD: MAX_LOGO_BYTES}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
MAX_LOGO_SIDE_PX = 2000  # width and height, read from the image header before decoding

# PDF generation is CPU- and memory-heavy: at most this many /generate jobs run at
# once (per process). Further requests wait up to JOB_WAIT_SECONDS, then get a 503.
MAX_CONCURRENT_JOBS = 2
JOB_WAIT_SECONDS = 5.0
BUSY_RETRY_AFTER_SECONDS = 60

MAX_ISSUES_SHOWN = 200  # rows of the issue table; the summary always counts them all
_PART_NUMBER = re.compile(r"[0-9]{1,4}")  # ASCII digits only

_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
# The browser's local date and time as DDMMYYYY_HHMM, used in the ZIP filename.
# ASCII digits only: the value goes into a response header.
_CLIENT_STAMP = re.compile(r"[0-9]{8}_[0-9]{4}")
STAMP_FORMAT = "%d%m%Y_%H%M"

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
DEFAULT_LOGO_PATH: Path = Path(__file__).parent / "static" / "brand" / "logo.png"

MSG_MALFORMED = "The upload could not be read. Reload the page and try again."
MSG_NO_FILE = "Choose a .csv or .xlsx file to upload."
MSG_UNSUPPORTED = "Unsupported file type. Upload a .csv or .xlsx file."
MSG_BODY_TOO_LARGE = (
    f"The upload is too large. Data files can be up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
    f"and logos up to {MAX_LOGO_BYTES // 1024} KB."
)
MSG_TOO_LARGE: dict[str, str] = {
    DATA_FIELD: (
        f"The data file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
        "Split it into smaller files and upload them one at a time."
    ),
    LOGO_FIELD: f"The logo is larger than {MAX_LOGO_BYTES // 1024} KB. Use a smaller image.",
}
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

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)


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
    """What the results partial shows: row count, number of parts, validation report."""

    row_count: int
    report: ValidationReport

    @property
    def parts(self) -> int:
        """How many ZIP files (parts of ``GENERATE_CHUNK_ROWS`` rows) the upload needs."""
        return pipeline.part_count(self.row_count)


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

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.files: dict[str, UploadedFile] = {}
        self._parts = 0
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
        self._limit = PART_LIMITS.get(self._name, MAX_FIELD_BYTES)
        if self._name == DATA_FIELD and self._filename:
            _check_extension(self._filename)  # before reading any of the file

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if len(self._data) + (end - start) > self._limit:
            raise UploadRejected(413, MSG_TOO_LARGE.get(self._name, MSG_FIELD_TOO_LARGE))
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


async def read_capped(request: Request) -> UploadForm:
    """Stream the multipart body into memory, enforcing the size caps as bytes arrive.

    Raises ``UploadRejected`` with 413 as soon as the data file passes
    ``MAX_UPLOAD_BYTES``, the logo passes ``MAX_LOGO_BYTES``, a text field passes
    ``MAX_FIELD_BYTES`` or the body passes ``MAX_BODY_BYTES``. The rest of the
    body is not read. A declared Content-Length above ``MAX_BODY_BYTES`` is
    refused before anything is read, and so is a data file with an unsupported
    extension (400). Nothing is written to disk.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise UploadRejected(400, MSG_MALFORMED)
        if int(declared) > MAX_BODY_BYTES:
            raise UploadRejected(413, MSG_BODY_TOO_LARGE)

    content_type, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadRejected(400, MSG_MALFORMED)

    collector = _FormCollector()
    received = 0
    try:
        parser = MultipartParser(boundary, collector.callbacks())  # type: ignore[arg-type]
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise UploadRejected(413, MSG_BODY_TOO_LARGE)
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


def _check(upload: UploadedFile) -> Results:
    """Parse and validate only. Raises ``ParseError``."""
    parsed, report = check_upload(upload.data, upload.filename)
    if not parsed.records:
        raise ParseError(MSG_NO_ROWS)
    return Results(row_count=len(parsed.records), report=report)


def _row_count(report: ValidationReport) -> int:
    """Rows read: valid records plus records excluded for errors."""
    return len(report.valid) + len({issue.row_number for issue in report.errors})


# --- Responses -----------------------------------------------------------------


def _form_values(defaults: Branding, form: UploadForm | None = None) -> dict[str, str]:
    """Text values to pre-fill the form with: the submitted ones, else the defaults."""
    fields = form.fields if form is not None else {}
    return {
        "company_name": fields.get("company_name", defaults.company_name),
        "primary_color": fields.get("primary_color", defaults.primary_hex),
        "footer_text": fields.get("footer_text", defaults.footer_text),
    }


def _context(**extra: Any) -> dict[str, Any]:
    return {
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_logo_bytes": MAX_LOGO_BYTES,
        "max_logo_kb": MAX_LOGO_BYTES // 1024,
        "max_rows": MAX_ROWS,
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
    return headers


# One semaphore per event loop: an asyncio.Semaphore is bound to the loop it first
# waits on. Uvicorn runs one loop per worker process, so in production this is one
# process-wide limit; tests that start their own loops get fresh semaphores.
_job_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _generate_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _job_slots.get(loop)
    if slots is None:
        slots = _job_slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    return slots


def _page(
    request: Request, form_values: dict[str, str], status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        _context(form=form_values, **extra),
        status_code=status_code,
        headers=_rejection_headers(status_code),
    )


def _partial(request: Request, status_code: int = 200, **extra: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_results.html",
        _context(**extra),
        status_code=status_code,
        headers=_rejection_headers(status_code),
    )


# --- Routes --------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, defaults: Annotated[Branding, Depends(get_branding)]
) -> HTMLResponse:
    """GET /: render the upload form."""
    return _page(request, _form_values(defaults))


@router.post("/validate", response_class=HTMLResponse)
async def validate_upload(request: Request) -> HTMLResponse:
    """POST /validate (HTMX): parse and validate only; return the ``_results.html`` partial."""
    try:
        upload = _data_file(await read_capped(request))
    except UploadRejected as exc:
        return _partial(request, exc.status_code, error=exc.message)
    try:
        results = await run_in_threadpool(_check, upload)
    except ParseError as exc:
        return _partial(request, error=str(exc))
    return _partial(request, results=results)


@router.post("/generate", response_model=None)
async def generate(
    request: Request, defaults: Annotated[Branding, Depends(get_branding)]
) -> Response:
    """POST /generate: validate the whole upload, then build the ZIP for one part of it.

    The form fields ``part`` (1-based) and ``parts`` say which part; both default
    to 1, which is right for uploads of up to ``GENERATE_CHUNK_ROWS`` rows. The
    page's script requests the parts one after another.

    Returns the ZIP when every row of the upload is valid, otherwise the full
    page with the results table (422), the reason the request was rejected
    (400/413, including a part that does not match the file), or a busy message
    (503) when no job slot frees up within ``JOB_WAIT_SECONDS``.

    The pipeline runs in a worker thread. The slot is held until that thread
    finishes, even if the client disconnects: the thread call is not abandoned
    on cancellation, so at most ``MAX_CONCURRENT_JOBS`` pipelines ever run.
    """
    form_values = _form_values(defaults)
    try:
        form = await read_capped(request)
        form_values = _form_values(defaults, form)
        upload = _data_file(form)
        part, parts = _part_number(form, "part"), _part_number(form, "parts")
        branding = _branding(form, defaults)
    except UploadRejected as exc:
        return _page(request, form_values, exc.status_code, error=exc.message)

    slots = _generate_slots()
    try:
        await asyncio.wait_for(slots.acquire(), JOB_WAIT_SECONDS)
    except TimeoutError:
        return _page(request, form_values, 503, error=MSG_BUSY)
    try:
        report, archive = await run_in_threadpool(
            process_upload, upload.data, upload.filename, branding, part=part, parts=parts
        )
    except ParseError as exc:
        return _page(request, form_values, 422, error=str(exc))
    except PartError:
        return _page(request, form_values, 400, error=MSG_BAD_PART)
    finally:
        slots.release()
    if archive is None:
        results = Results(row_count=_row_count(report), report=report)
        return _page(request, form_values, 422, results=results)
    if not report.valid:
        return _page(request, form_values, 422, error=MSG_NO_ROWS)

    filename = _zip_filename(_batch_stamp(form), part, parts)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

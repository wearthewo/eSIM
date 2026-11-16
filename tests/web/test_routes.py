from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import re
import struct
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import starlette.formparsers
from fastapi.testclient import TestClient
from PIL import Image

from app.core import parser, pipeline, zipper
from app.core.models import Branding
from app.web import routes
from app.web.main import SECURITY_HEADERS, create_app
from tests.conftest import FIXTURES_DIR, OVERSIZE_CAP_BYTES, synthetic_csv, synthetic_iccid
from tests.core.test_email import MISSING_EMAIL_MESSAGE
from tests.core.test_email import _valid_fixture as valid_emails_csv

FORM = {
    "company_name": "Example Telecom (test)",
    "primary_color": "#1a4d8f",
    "footer_text": "help@example.invalid",
}
VALID_MINIMAL_ICCIDS = ["8999000000000000013", "8999000000000000021", "8999000000000000039"]
SECRET_COLUMNS = ("activation_code", "matching_id", "confirmation_code")
CHUNK = 64 * 1024
BOUNDARY = "test-boundary-7d3a"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _image(fmt: str, size: tuple[int, int] = (8, 4), **save: object) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (26, 77, 143)).save(buf, format=fmt, **save)
    return buf.getvalue()


def _csv_file(name: str, data: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, "text/csv")}


def _zip_name(response) -> str:
    match = re.fullmatch(r'attachment; filename="([^"]+)"', response.headers["content-disposition"])
    assert match, response.headers["content-disposition"]
    return match.group(1)


def _assert_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


def _secret_values(data: bytes) -> set[str]:
    """Activation codes, matching IDs and confirmation codes in a synthetic CSV."""
    text = data.decode("utf-8-sig", errors="replace")
    delimiter = (
        ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
    )
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return set()
    header = [h.strip().lower() for h in rows[0]]
    indices = [i for i, h in enumerate(header) if h in SECRET_COLUMNS]
    return {row[i].strip() for row in rows[1:] for i in indices if i < len(row) and row[i].strip()}


def _assert_no_secrets(html: str, secrets: set[str]) -> None:
    leaked = sorted(s for s in secrets if s in html)
    assert not leaked, f"secret values in HTML: {len(leaked)}"


# --- Driving the ASGI app with a chunked body -------------------------------------


def _multipart(*parts: tuple[str, str | None, bytes]) -> bytes:
    """Encode ``(name, filename or None, data)`` parts as multipart/form-data."""
    out = bytearray()
    for name, filename, data in parts:
        out += f"--{BOUNDARY}\r\n".encode()
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += f"Content-Disposition: {disposition}\r\n".encode()
        if filename is not None:
            out += b"Content-Type: application/octet-stream\r\n"
        out += b"\r\n" + data + b"\r\n"
    out += f"--{BOUNDARY}--\r\n".encode()
    return bytes(out)


def _asgi_post(
    path: str, body: bytes, content_length: int | None = None
) -> tuple[int, dict[str, str], bytes, int, int]:
    """POST ``body`` in 64 KB chunks without buffering it first.

    Returns ``(status, headers, response_body, chunks_read, chunks_total)``.
    ``content_length=None`` sends no Content-Length (like a chunked upload).
    """
    chunks = [body[i : i + CHUNK] for i in range(0, len(body), CHUNK)]
    read = 0
    sent: list[dict] = []

    async def receive() -> dict:
        nonlocal read
        if read < len(chunks):
            read += 1
            return {
                "type": "http.request",
                "body": chunks[read - 1],
                "more_body": read < len(chunks),
            }
        await asyncio.sleep(3600)  # the client never disconnects mid-test
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        sent.append(message)

    headers = [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(create_app()(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
    response_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], response_headers, response_body, read, len(chunks)


# --- App, index, headers ------------------------------------------------------------


def test_app_debug_off() -> None:
    assert create_app().debug is False


def test_oversize_fixture_matches_cap(oversize_upload: bytes) -> None:
    assert OVERSIZE_CAP_BYTES == routes.MAX_UPLOAD_BYTES
    assert len(oversize_upload) == routes.MAX_UPLOAD_BYTES + 1


def test_index_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    _assert_security_headers(response)
    html = response.text
    for field in ("file", "company_name", "primary_color", "logo", "footer_text"):
        assert f'name="{field}"' in html
    assert 'action="/generate"' in html
    assert 'hx-post="/validate"' in html
    assert "processed in memory and never stored" in html
    for element in ('id="progress"', 'id="generating"', 'id="cancel"', 'id="retry"'):
        assert element in html
    for hidden in ("client_stamp", "part", "parts"):
        assert f'type="hidden" name="{hidden}"' in html
    assert "download_token" not in html
    assert "10000 rows" in html and "parts of 500" in html
    assert 'id="download"' not in html  # only after a successful validation


def test_index_is_csp_clean(client: TestClient) -> None:
    html = client.get("/").text
    csp = client.get("/").headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son\w+=", html), "inline event handler"
    assert "hx-on" not in html
    for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert client.get(src).status_code == 200, src


@pytest.mark.parametrize("path", ["/static/htmx.min.js", "/static/app.js", "/nope"])
def test_security_headers_on_every_response(client: TestClient, path: str) -> None:
    _assert_security_headers(client.get(path))


def test_unhandled_error_is_generic_with_headers() -> None:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("row 2 LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    _assert_security_headers(response)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_get_branding() -> None:
    branding = routes.get_branding()
    assert branding.logo_png is not None and branding.logo_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert branding.primary_hex == routes.DEFAULT_PRIMARY_HEX
    assert branding.company_name == ""


# --- POST /validate -------------------------------------------------------------------


def test_validate_valid(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/validate", files=_csv_file("valid_minimal.csv", fixture_bytes("valid_minimal.csv"))
    )
    assert response.status_code == 200
    _assert_security_headers(response)
    html = response.text
    assert "<html" not in html  # a partial
    assert "3 rows read: 0 errors, 0 warnings." in html
    assert "<table" not in html
    assert 'id="download"' in html


def test_validate_xlsx(client: TestClient, valid_xlsx: bytes) -> None:
    response = client.post(
        "/validate", files={"file": ("batch.XLSX", valid_xlsx, "application/octet-stream")}
    )
    assert response.status_code == 200
    assert "2 rows read: 0 errors" in response.text
    assert 'id="download"' in response.text


def test_validate_invalid(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/validate", files=_csv_file("mixed.csv", fixture_bytes("mixed_valid_invalid.csv"))
    )
    assert response.status_code == 200
    html = response.text
    assert "4 rows read: 2 errors, 0 warnings." in html
    assert "<th>Row</th><th>Field</th><th>Message</th><th>Severity</th>" in html
    assert re.search(
        r'<tr class="error"><td>5</td><td>activation_code</td><td>[^<]+</td><td>error</td></tr>',
        html,
    )
    assert 'id="download"' not in html


def test_validate_warning_only(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/validate", files=_csv_file("w.csv", fixture_bytes("warning_iccid_prefix.csv"))
    )
    assert response.status_code == 200
    html = response.text
    assert "1 row read: 0 errors, 1 warning." in html
    assert '<tr class="warning"><td>2</td><td>iccid</td>' in html
    assert "89 telecom prefix" in html
    assert 'id="download"' in html


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("missing_activation_columns.csv", "activation"),
        ("empty.csv", "no data rows"),  # header only
        ("not_utf8.csv", "not valid UTF-8"),
    ],
)
def test_validate_parse_error(client: TestClient, fixture_bytes, name: str, expected: str) -> None:
    response = client.post("/validate", files=_csv_file(name, fixture_bytes(name)))
    assert response.status_code == 200
    html = response.text
    assert 'class="error" role="alert"' in html
    assert expected in html
    assert "<table" not in html and 'id="download"' not in html


def test_validate_parse_error_xlsx(client: TestClient, numeric_iccid_xlsx: bytes) -> None:
    response = client.post(
        "/validate", files={"file": ("n.xlsx", numeric_iccid_xlsx, "application/octet-stream")}
    )
    assert response.status_code == 200
    assert 'role="alert"' in response.text and 'id="download"' not in response.text


def test_validate_header_only(client: TestClient) -> None:
    response = client.post("/validate", files=_csv_file("h.csv", b"iccid,activation_code\n"))
    assert response.status_code == 200
    assert routes.MSG_NO_ROWS in response.text
    assert 'id="download"' not in response.text


@pytest.mark.parametrize("name", ["unsupported.txt", "data.csv.exe", "noextension", "data.xls"])
def test_validate_unsupported_extension(client: TestClient, fixture_bytes, name: str) -> None:
    response = client.post("/validate", files=_csv_file(name, fixture_bytes("unsupported.txt")))
    assert response.status_code == 400
    _assert_security_headers(response)
    assert routes.MSG_UNSUPPORTED in response.text


def test_validate_without_file(client: TestClient) -> None:
    assert client.post("/validate", data={"company_name": "x"}, files={}).status_code == 400
    response = client.post("/validate", files={"file": ("", b"", "application/octet-stream")})
    assert response.status_code == 400
    assert routes.MSG_NO_FILE in response.text


def test_validate_ignores_branding_fields(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/validate",
        data={"company_name": "", "primary_color": "nope"},
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("fake.png", b"not an image", "image/png"),
        },
    )
    assert response.status_code == 200
    assert 'id="download"' in response.text


# --- POST /generate -------------------------------------------------------------------


def test_generate_returns_zip(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/generate",
        data=FORM,
        files=_csv_file("valid_minimal.csv", fixture_bytes("valid_minimal.csv")),
    )
    assert response.status_code == 200
    _assert_security_headers(response)
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert re.fullmatch(r"esim_batch_[0-9]{8}_[0-9]{4}\.zip", _zip_name(response))
    assert "set-cookie" not in response.headers
    pdfs = [f"esim_{iccid}.pdf" for iccid in VALID_MINIMAL_ICCIDS]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [*pdfs, zipper.MANIFEST_NAME]
        for name in pdfs:
            assert zf.read(name).startswith(b"%PDF-")
        manifest = list(csv.reader(io.StringIO(zf.read(zipper.MANIFEST_NAME).decode("utf-8-sig"))))
    assert manifest == [
        list(zipper.MANIFEST_COLUMNS),
        *[
            [str(row), iccid, pdf]
            for row, iccid, pdf in zip((2, 3, 4), VALID_MINIMAL_ICCIDS, pdfs, strict=True)
        ],
    ]


def test_generate_filename_uses_client_stamp(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/generate",
        data={**FORM, "client_stamp": "16112026_0935"},
        files=_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
    )
    assert response.status_code == 200
    assert _zip_name(response) == "esim_batch_16112026_0935.zip"


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "",
        "16112026_093",
        "16112026_09350",
        "161120260935",
        "2026-11-16_0935",
        "20261116T093500Z",
        " 16112026_0935",
        "16112026_0935\n",
        '16112026_0935"; x="',
        "../../etc_passwd",
        "١٦١١٢٠٢٦_٠٩٣٥",  # Unicode digits are not accepted
    ],
)
def test_generate_filename_falls_back_to_utc(
    client: TestClient, fixture_bytes, stamp: str | None
) -> None:
    data = FORM if stamp is None else {**FORM, "client_stamp": stamp}
    before = datetime.now(UTC)
    response = client.post(
        "/generate", data=data, files=_csv_file("v.csv", fixture_bytes("valid_minimal.csv"))
    )
    after = datetime.now(UTC)
    assert response.status_code == 200
    expected = {f"esim_batch_{moment:%d%m%Y_%H%M}.zip" for moment in (before, after)}
    assert _zip_name(response) in expected


def test_generate_xlsx_and_warnings_allowed(
    client: TestClient, valid_xlsx: bytes, fixture_bytes
) -> None:
    xlsx = client.post(
        "/generate", data=FORM, files={"file": ("b.xlsx", valid_xlsx, "application/octet-stream")}
    )
    assert xlsx.status_code == 200 and xlsx.headers["content-type"] == "application/zip"
    warn = client.post(
        "/generate", data=FORM, files=_csv_file("w.csv", fixture_bytes("warning_iccid_prefix.csv"))
    )
    assert warn.status_code == 200 and warn.headers["content-type"] == "application/zip"


def _first_pdf(response) -> bytes:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return zf.read(next(n for n in zf.namelist() if n.endswith(".pdf")))


@pytest.mark.parametrize(
    ("fmt", "name"), [("PNG", "logo.png"), ("JPEG", "logo.jpg"), ("PNG", "logo.jpeg")]
)
def test_generate_with_logo(client: TestClient, fixture_bytes, fmt: str, name: str) -> None:
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": (name, _image(fmt), "application/octet-stream"),
        },
    )
    assert response.status_code == 200
    assert b"/Subtype /Image" in _first_pdf(response)


def test_generate_without_logo_uses_default(fixture_bytes) -> None:
    body = _multipart(
        *[(k, None, v.encode()) for k, v in FORM.items()],
        ("file", "v.csv", fixture_bytes("valid_minimal.csv")),
        ("logo", "", b""),  # what a browser sends when no logo is chosen
    )
    status, headers, response, _, _ = _asgi_post("/generate", body, content_length=len(body))
    assert status == 200 and headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response)) as zf:
        assert b"/Subtype /Image" in zf.read("esim_8999000000000000013.pdf")


def test_generate_branding_override() -> None:
    app = create_app()
    app.dependency_overrides[routes.get_branding] = lambda: Branding(company_name="Default (test)")
    response = TestClient(app).post(
        "/generate",
        data=FORM,
        files=_csv_file("v.csv", (FIXTURES_DIR / "valid_minimal.csv").read_bytes()),
    )
    assert response.status_code == 200
    assert b"/Subtype /Image" not in _first_pdf(response)  # override has no logo


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a\x01\x00\x01\x00",  # a GIF named .png
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"PNG but not really",
        b"",
    ],
)
def test_generate_rejects_fake_logo(client: TestClient, fixture_bytes, data: bytes) -> None:
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("logo.png", data, "image/png"),
        },
    )
    assert response.status_code == 400
    assert routes.MSG_BAD_LOGO in response.text
    assert "<html" in response.text  # full page


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"company_name": "   "}, routes.MSG_COMPANY_MISSING),
        ({"company_name": "x" * 101}, routes.MSG_COMPANY_LONG),
        ({"footer_text": "x" * 201}, routes.MSG_FOOTER_LONG),
        ({"company_name": "Bad\x07Co"}, routes.MSG_CONTROL_CHARS),
        ({"primary_color": "red"}, routes.MSG_BAD_COLOUR),
        ({"primary_color": "#12345"}, routes.MSG_BAD_COLOUR),
    ],
)
def test_generate_rejects_bad_branding(
    client: TestClient, fixture_bytes, override, message
) -> None:
    response = client.post(
        "/generate",
        data={**FORM, **override},
        files=_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
    )
    assert response.status_code == 400
    assert message in response.text


def test_generate_invalid_returns_page(client: TestClient, fixture_bytes) -> None:
    form = {**FORM, "company_name": '<b>Acme</b> "test"'}
    response = client.post(
        "/generate", data=form, files=_csv_file("m.csv", fixture_bytes("mixed_valid_invalid.csv"))
    )
    assert response.status_code == 422
    _assert_security_headers(response)
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<html" in html and 'action="/generate"' in html
    assert "4 rows read: 2 errors, 0 warnings." in html
    assert '<tr class="error"><td>5</td><td>activation_code</td>' in html
    assert 'id="download"' not in html
    # Submitted text values are re-filled, escaped.
    assert 'value="&lt;b&gt;Acme&lt;/b&gt; &#34;test&#34;"' in html
    assert "<b>Acme</b>" not in html


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        (
            "missing.csv",
            (FIXTURES_DIR / "missing_activation_columns.csv").read_bytes(),
            "activation",
        ),
        ("header.csv", b"iccid,activation_code\n", routes.MSG_NO_ROWS),
        ("too_many.csv", synthetic_csv(10_001), "Too many rows"),
    ],
)
def test_generate_parse_error_returns_page(
    client: TestClient, name: str, data: bytes, expected: str
) -> None:
    response = client.post("/generate", data=FORM, files=_csv_file(name, data))
    assert response.status_code == 422
    assert "<html" in response.text
    assert expected in response.text


def test_generate_unsupported_and_missing_file(client: TestClient) -> None:
    unsupported = client.post("/generate", data=FORM, files=_csv_file("x.txt", b"hello"))
    assert unsupported.status_code == 400 and routes.MSG_UNSUPPORTED in unsupported.text
    missing = client.post(
        "/generate", data=FORM, files={"file": ("", b"", "application/octet-stream")}
    )
    assert missing.status_code == 400 and routes.MSG_NO_FILE in missing.text


# --- Size limits: 413 without reading the whole body ------------------------------------


@pytest.mark.parametrize("path", ["/validate", "/generate"])
def test_oversize_file_413(client: TestClient, oversize_upload: bytes, path: str) -> None:
    response = client.post(path, data=FORM, files=_csv_file("big.csv", oversize_upload))
    assert response.status_code == 413
    _assert_security_headers(response)
    assert response.headers["connection"] == "close"
    assert routes.MSG_TOO_LARGE[routes.DATA_FIELD] in response.text
    assert ("<html" in response.text) is (path == "/generate")


def test_file_at_cap_is_accepted(client: TestClient, oversize_upload: bytes) -> None:
    response = client.post("/validate", files=_csv_file("cap.csv", oversize_upload[:-1]))
    assert response.status_code == 200


def test_oversize_logo_413(client: TestClient, fixture_bytes) -> None:
    logo = _image("PNG") + b"\x00" * routes.MAX_LOGO_BYTES
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("l.png", logo, "image/png"),
        },
    )
    assert response.status_code == 413
    assert routes.MSG_TOO_LARGE[routes.LOGO_FIELD] in response.text


@pytest.mark.parametrize("path", ["/validate", "/generate"])
def test_oversize_file_stops_reading_early(oversize_upload: bytes, path: str) -> None:
    body = _multipart(("company_name", None, b"Test"), ("file", "big.csv", oversize_upload * 2))
    status, headers, response, read, total = _asgi_post(path, body)
    assert status == 413
    assert routes.MSG_TOO_LARGE[routes.DATA_FIELD].encode() in response
    # Stopped right after crossing the 5 MB cap: one chunk past it, at most.
    assert read * CHUNK <= routes.MAX_UPLOAD_BYTES + 2 * CHUNK
    assert read < total


def test_oversize_logo_stops_reading_early(fixture_bytes) -> None:
    logo = _image("PNG") + b"\x00" * (4 * 1024 * 1024)
    body = _multipart(
        *[(k, None, v.encode()) for k, v in FORM.items()],
        ("file", "v.csv", fixture_bytes("valid_minimal.csv")),
        ("logo", "logo.png", logo),
    )
    status, _, response, read, total = _asgi_post("/generate", body)
    assert status == 413
    assert routes.MSG_TOO_LARGE[routes.LOGO_FIELD].encode() in response
    assert read * CHUNK <= routes.MAX_LOGO_BYTES + 2 * CHUNK
    assert read < total


def test_declared_content_length_over_limit_reads_nothing() -> None:
    body = _multipart(("file", "v.csv", b"iccid\n"))
    status, headers, response, read, _ = _asgi_post(
        "/validate", body, content_length=routes.MAX_BODY_BYTES + 1
    )
    assert status == 413
    assert read == 0
    assert routes.MSG_BODY_TOO_LARGE.encode() in response


def test_unsupported_extension_rejected_before_reading_file() -> None:
    body = _multipart(("file", "big.txt", b"x" * (3 * 1024 * 1024)))
    status, _, response, read, total = _asgi_post("/validate", body)
    assert status == 400
    assert routes.MSG_UNSUPPORTED.encode() in response
    assert read == 1 < total


def test_oversize_text_field_413() -> None:
    body = _multipart(("company_name", None, b"x" * (routes.MAX_FIELD_BYTES + 1)))
    status, _, response, _, _ = _asgi_post("/generate", body)
    assert status == 413
    assert routes.MSG_FIELD_TOO_LARGE.encode() in response


def test_unknown_file_part_capped_like_text_field() -> None:
    body = _multipart(("other", "x.bin", b"x" * (routes.MAX_FIELD_BYTES + 1)))
    status, _, response, _, _ = _asgi_post("/validate", body)
    assert status == 413 and routes.MSG_FIELD_TOO_LARGE.encode() in response


def test_whole_body_cap_without_content_length(fixture_bytes) -> None:
    # Every part is under its cap, but the epilogue after the closing boundary
    # (skipped by the multipart parser) pushes the body over MAX_BODY_BYTES.
    epilogue = b"x" * (routes.MAX_BODY_BYTES + CHUNK)
    body = _multipart(("file", "v.csv", fixture_bytes("valid_minimal.csv"))) + epilogue
    status, _, response, read, total = _asgi_post("/validate", body)
    assert status == 413
    assert routes.MSG_BODY_TOO_LARGE.encode() in response
    assert read < total


# --- Malformed requests ------------------------------------------------------------------


def test_too_many_parts_400() -> None:
    body = _multipart(*[(f"f{i}", None, b"x") for i in range(routes.MAX_PARTS + 1)])
    status, _, response, _, _ = _asgi_post("/validate", body)
    assert status == 400 and routes.MSG_MALFORMED.encode() in response


def test_duplicate_field_400(fixture_bytes) -> None:
    csv_bytes = fixture_bytes("valid_minimal.csv")
    body = _multipart(("file", "a.csv", csv_bytes), ("file", "b.csv", csv_bytes))
    status, _, _, _, _ = _asgi_post("/validate", body)
    assert status == 400


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b"iccid,activation_code\n", "headers": {"content-type": "text/csv"}},
        {"data": {"file": "not a file part"}},  # urlencoded
        {"content": b"garbage", "headers": {"content-type": "multipart/form-data"}},  # no boundary
        {"content": b"garbage", "headers": {"content-type": "multipart/form-data; boundary=x"}},
    ],
)
def test_malformed_requests_400(client: TestClient, kwargs) -> None:
    response = client.post("/validate", **kwargs)
    assert response.status_code == 400
    _assert_security_headers(response)
    assert "Traceback" not in response.text


# --- Never on disk, never in HTML -------------------------------------------------------------


def test_uploads_never_touch_disk(client: TestClient, monkeypatch) -> None:
    def no_disk(*args, **kwargs):
        raise AssertionError("upload written to a temporary file")

    for name in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, no_disk)
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", no_disk)

    # Over 1 MB: Starlette's own form parser would spool this to disk.
    padding = "x" * 100_000  # below the csv module's 128 KB field limit
    rows = [
        f"{synthetic_iccid(i)},LPA:1$smdp.example.invalid$TEST-{i:04d}-DISK,{padding}"
        for i in range(1, 22)
    ]
    data = ("iccid,activation_code,notes\n" + "\n".join(rows) + "\n").encode()
    assert len(data) > 2 * 1024 * 1024

    response = client.post("/validate", files=_csv_file("big.csv", data))
    assert response.status_code == 200 and "21 rows read" in response.text
    response = client.post("/generate", data=FORM, files=_csv_file("big.csv", data))
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"


def _invalid_full_csv() -> bytes:
    """valid_full.csv plus one row with a bad IMSI, so /generate returns HTML."""
    base = (FIXTURES_DIR / "valid_full.csv").read_bytes().rstrip(b"\r\n")
    row = (
        f"{synthetic_iccid(15)},LPA:1$smdp.example.invalid$TEST-0015-AAAA-BBBB,CONF0015,"
        "12345,+306900000015,Test Customer 15,Test Plan 5GB"
    )
    return base + b"\n" + row.encode() + b"\n"


SECRET_CASES = sorted(p.name for p in FIXTURES_DIR.glob("*.csv")) + ["invalid_full.csv"]


@pytest.mark.parametrize("name", SECRET_CASES)
def test_no_secrets_in_html(client: TestClient, name: str) -> None:
    data = _invalid_full_csv() if name == "invalid_full.csv" else (FIXTURES_DIR / name).read_bytes()
    secrets = _secret_values(data)
    for path in ("/validate", "/generate"):
        response = client.post(path, data=FORM, files=_csv_file(name, data))
        if response.headers["content-type"].startswith("text/html"):
            _assert_no_secrets(response.text, secrets)
    _assert_no_secrets(client.get("/").text, secrets)


def test_secret_check_covers_codes() -> None:
    """Guard for the test above: the extractor really finds the secret values."""
    secrets = _secret_values(_invalid_full_csv())
    assert "LPA:1$smdp.example.invalid$TEST-0010-AAAA-BBBB" in secrets
    assert "CONF0015" in secrets
    split = _secret_values((FIXTURES_DIR / "valid_split_smdp.csv").read_bytes())
    assert "TEST-0020-SPLIT" in split


def test_invalid_full_page_shows_rows_only(client: TestClient) -> None:
    response = client.post("/generate", data=FORM, files=_csv_file("f.csv", _invalid_full_csv()))
    assert response.status_code == 422
    html = response.text
    assert '<tr class="error"><td>7</td><td>imsi</td>' in html
    for value in ("TEST-00", "CONF00", "smdp.example.invalid", "Test Customer", "+3069", "0010100"):
        assert value not in html


# --- Logo dimensions, read from the header before decoding ---------------------------


def _png_header(width: int, height: int) -> bytes:
    """A 33-byte PNG: signature plus an IHDR chunk claiming ``width`` x ``height``."""
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        routes.PNG_SIGNATURE
        + struct.pack(">I", 13)
        + ihdr
        + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF)
    )


def _jpeg_header(width: int, height: int, marker: int = 0xC0) -> bytes:
    """A tiny JPEG: SOI, an APP0 segment, a frame header claiming the size, EOI."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    components = b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof = bytes([0xFF, marker]) + struct.pack(">HBHHB", 17, 8, height, width, 3) + components
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.mark.parametrize(
    "logo",
    [
        _png_header(50000, 50000),
        _jpeg_header(50000, 50000),
        _jpeg_header(50000, 50000, marker=0xC2),  # progressive
        _png_header(2001, 1),
        _jpeg_header(1, 2001),
    ],
    ids=["png-50000", "jpeg-50000", "progressive-jpeg-50000", "png-2001-wide", "jpeg-2001-tall"],
)
def test_logo_too_many_pixels_rejected_before_decoding(
    client: TestClient, fixture_bytes, monkeypatch, logo: bytes
) -> None:
    assert len(logo) < 100

    def never(*args, **kwargs):
        raise AssertionError("pipeline must not run for an oversize logo")

    monkeypatch.setattr(routes, "process_upload", never)
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("logo.png", logo, "image/png"),
        },
    )
    assert response.status_code == 400
    assert routes.MSG_LOGO_DIMENSIONS in response.text
    assert "<html" in response.text


@pytest.mark.parametrize(("fmt", "size"), [("PNG", (2000, 8)), ("JPEG", (8, 2000))])
def test_logo_at_dimension_limit_accepted(client: TestClient, fixture_bytes, fmt, size) -> None:
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("logo", _image(fmt, size), "application/octet-stream"),
        },
    )
    assert response.status_code == 200
    assert b"/Subtype /Image" in _first_pdf(response)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_image("PNG", (37, 11)), (37, 11)),
        (_image("JPEG", (37, 11)), (37, 11)),
        (_image("JPEG", (37, 11), progressive=True), (37, 11)),
        (_image("JPEG", (37, 11), exif=Image.Exif()), (37, 11)),
        (_png_header(50000, 50000), (50000, 50000)),
        (_jpeg_header(50000, 40000), (50000, 40000)),
    ],
    ids=["png", "jpeg", "progressive", "jpeg-exif", "png-header", "jpeg-header"],
)
def test_logo_dimensions(data: bytes, expected: tuple[int, int]) -> None:
    assert routes.logo_dimensions(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        routes.PNG_SIGNATURE,  # no IHDR
        routes.PNG_SIGNATURE + b"\x00\x00\x00\x0dIDAT" + b"\x00" * 13,  # first chunk not IHDR
        _png_header(0, 10),
        b"\xff\xd8\xff",  # SOI only
        b"\xff\xd8\xff\xd9",  # no frame header
        b"\xff\xd8\xff\xda\x00\x02",  # image data before any frame header
        _jpeg_header(0, 10),
        _jpeg_header(50000, 50000)[:30],  # truncated frame header
        b"\xff\xd8\xff\xe0\x00\x01",  # segment length < 2
        b"\xff\xd8\x00\x00",  # not a marker
        b"GIF89a\x01\x00\x01\x00",
    ],
)
def test_logo_dimensions_unreadable(data: bytes) -> None:
    assert routes.logo_dimensions(data) is None


def test_bad_logo_header_rejected(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/generate",
        data=FORM,
        files={
            **_csv_file("v.csv", fixture_bytes("valid_minimal.csv")),
            "logo": ("logo.jpg", b"\xff\xd8\xff\xd9", "image/jpeg"),
        },
    )
    assert response.status_code == 400
    assert routes.MSG_BAD_LOGO in response.text


# --- Concurrency: worker threads and the job limit ---------------------------------------


class _Gate:
    """Stand-in for a pipeline function: blocks its worker thread until ``release`` is set.

    If the route called it on the event loop instead, the loop would freeze and
    the tests below would time out and fail.
    """

    def __init__(self, real) -> None:
        self.real = real
        self.release = threading.Event()
        self._entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        self._entered.release()
        try:
            if not self.release.wait(5):
                raise AssertionError("gate never released")
            return self.real(*args, **kwargs)
        finally:
            with self._lock:
                self.running -= 1

    async def entered(self, count: int = 1) -> bool:
        """Wait (off the loop) until ``count`` more calls have started."""
        for _ in range(count):
            if not await asyncio.to_thread(self._entered.acquire, timeout=5):
                return False
        return True


def _async_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=10)


def _post_generate(client: httpx.AsyncClient, data: bytes):
    return client.post("/generate", data=FORM, files=_csv_file("v.csv", data))


@pytest.mark.parametrize(
    ("path", "target"), [("/generate", "process_upload"), ("/validate", "check_upload")]
)
def test_pipeline_runs_off_the_event_loop(monkeypatch, fixture_bytes, path, target) -> None:
    gate = _Gate(getattr(routes, target))
    monkeypatch.setattr(routes, target, gate)
    data = fixture_bytes("valid_minimal.csv")

    async def scenario() -> None:
        async with _async_client() as client:
            job = asyncio.create_task(client.post(path, data=FORM, files=_csv_file("v.csv", data)))
            assert await gate.entered()
            index = await asyncio.wait_for(client.get("/"), 2)
            assert index.status_code == 200
            assert not job.done()
            gate.release.set()
            assert (await job).status_code == 200

    asyncio.run(scenario())


def test_generate_limited_to_concurrent_jobs(monkeypatch, fixture_bytes) -> None:
    assert routes.MAX_CONCURRENT_JOBS == 2
    assert routes.JOB_WAIT_SECONDS == 5.0
    monkeypatch.setattr(routes, "JOB_WAIT_SECONDS", 0.2)
    gate = _Gate(routes.process_upload)
    monkeypatch.setattr(routes, "process_upload", gate)
    data = fixture_bytes("valid_minimal.csv")

    async def scenario() -> None:
        async with _async_client() as client:
            jobs = [asyncio.create_task(_post_generate(client, data)) for _ in range(2)]
            assert await gate.entered(2)

            busy = await asyncio.wait_for(_post_generate(client, data), 5)
            assert busy.status_code == 503
            assert routes.MSG_BUSY in busy.text
            assert "<html" in busy.text and 'action="/generate"' in busy.text
            assert busy.headers["retry-after"] == "60"
            assert busy.headers["cache-control"] == "no-store"
            _assert_security_headers(busy)
            assert gate.peak == 2  # the third request never reached the pipeline

            gate.release.set()
            assert [(await job).status_code for job in jobs] == [200, 200]
            assert (await _post_generate(client, data)).status_code == 200

    asyncio.run(scenario())


def test_generate_waits_for_a_free_slot(monkeypatch, fixture_bytes) -> None:
    gate = _Gate(routes.process_upload)
    monkeypatch.setattr(routes, "process_upload", gate)
    data = fixture_bytes("valid_minimal.csv")

    async def scenario() -> None:
        async with _async_client() as client:
            jobs = [asyncio.create_task(_post_generate(client, data)) for _ in range(2)]
            assert await gate.entered(2)
            waiting = asyncio.create_task(_post_generate(client, data))
            await asyncio.sleep(0.3)
            assert not waiting.done() and gate.running == 2  # queued, not in the pipeline
            gate.release.set()  # well within JOB_WAIT_SECONDS
            responses = await asyncio.gather(*jobs, waiting)
            assert [r.status_code for r in responses] == [200, 200, 200]
            assert responses[2].headers["content-type"] == "application/zip"
            assert gate.peak == 2

    asyncio.run(scenario())


def test_job_slots_released_after_failures(monkeypatch, fixture_bytes) -> None:
    monkeypatch.setattr(routes, "JOB_WAIT_SECONDS", 0.1)
    real = routes.process_upload
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("synthetic failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "process_upload", flaky)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        for _ in range(2):
            failed = client.post("/generate", data=FORM, files=_csv_file("v.csv", b"x"))
            assert failed.status_code == 500
        for _ in range(2):
            parse_error = client.post(
                "/generate", data=FORM, files=_csv_file("h.csv", b"iccid,activation_code\n")
            )
            assert parse_error.status_code == 422
        ok = client.post(
            "/generate", data=FORM, files=_csv_file("v.csv", fixture_bytes("valid_minimal.csv"))
        )
        assert ok.status_code == 200


# --- Email column and mailmerge.csv -------------------------------------------------------

EMAIL_FIXTURE = "emails_greek_mixed.csv"
EMAIL_PARTS = ("maria.p", "Giorgos.N", "eleni.example", "mail.example.invalid")


def test_validate_email_column_shows_error_and_warning(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/validate", files=_csv_file(EMAIL_FIXTURE, fixture_bytes(EMAIL_FIXTURE))
    )
    assert response.status_code == 200
    html = response.text
    assert "5 rows read: 1 error, 1 warning." in html
    assert '<tr class="error"><td>4</td><td>email</td>' in html
    assert f'<tr class="warning"><td>5</td><td>email</td><td>{MISSING_EMAIL_MESSAGE}</td>' in html
    assert 'id="download"' not in html
    assert "example.invalid" not in html  # no address is ever echoed
    for part in EMAIL_PARTS:
        assert part not in html


def test_validate_missing_email_is_only_a_warning(client: TestClient) -> None:
    response = client.post("/validate", files=_csv_file("emails.csv", valid_emails_csv()))
    assert response.status_code == 200
    assert "4 rows read: 0 errors, 1 warning." in response.text
    assert 'id="download"' in response.text


def test_generate_with_email_column_adds_mailmerge(client: TestClient) -> None:
    response = client.post("/generate", data=FORM, files=_csv_file("e.csv", valid_emails_csv()))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
        assert names[-2:] == [zipper.MANIFEST_NAME, zipper.MAILMERGE_NAME]
        raw = zf.read(zipper.MAILMERGE_NAME)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0] == list(zipper.MAILMERGE_COLUMNS)
    assert [r[0] for r in rows[1:]] == [
        "maria.p@example.invalid",
        "Giorgos.N@mail.example.invalid",
        "",
        "maria.p@example.invalid",
    ]
    assert rows[1][1] == "Μαρία Παπαδοπούλου"
    assert [r[4] for r in rows[1:]] == names[:-2]
    for marker in ("LPA:1$", "TEST-0", "CONF0", "smdp.example.invalid"):
        assert marker not in raw.decode("utf-8-sig")


def test_generate_without_email_column_has_no_mailmerge(client: TestClient, fixture_bytes) -> None:
    response = client.post(
        "/generate", data=FORM, files=_csv_file("v.csv", fixture_bytes("valid_full.csv"))
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zipper.MAILMERGE_NAME not in zf.namelist()


def test_generate_email_error_page_shows_no_addresses(client: TestClient, fixture_bytes) -> None:
    form = {**FORM, "footer_text": ""}
    response = client.post(
        "/generate", data=form, files=_csv_file(EMAIL_FIXTURE, fixture_bytes(EMAIL_FIXTURE))
    )
    assert response.status_code == 422
    html = response.text
    assert '<tr class="error"><td>4</td><td>email</td>' in html
    assert "example.invalid" not in html
    for part in EMAIL_PARTS:
        assert part not in html


# --- Large orders: parts ------------------------------------------------------------------


def _load_make_big_csv():
    spec = importlib.util.spec_from_file_location(
        "make_big_csv", Path(__file__).resolve().parents[2] / "scripts" / "make_big_csv.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _post_part(client: TestClient, data: bytes, part: object, parts: object, **extra):
    form = {**FORM, "client_stamp": "16112026_0935", "part": str(part), "parts": str(parts)}
    return client.post("/generate", data={**form, **extra}, files=_csv_file("order.csv", data))


def _manifest(response) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return list(csv.reader(io.StringIO(zf.read(zipper.MANIFEST_NAME).decode("utf-8-sig"))))


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


def test_limits_shown_to_the_web_layer() -> None:
    assert parser.MAX_ROWS == 10_000
    assert pipeline.GENERATE_CHUNK_ROWS == 500
    assert routes.MAX_CONCURRENT_JOBS == 2


def test_ten_thousand_rows_with_all_columns_fit_under_the_upload_cap() -> None:
    data = _load_make_big_csv().make_csv(parser.MAX_ROWS)
    assert len(data) < routes.MAX_UPLOAD_BYTES
    assert routes.MAX_UPLOAD_BYTES == 5 * 1024 * 1024


def test_validate_ten_thousand_rows_reports_twenty_parts(client: TestClient) -> None:
    data = _load_make_big_csv().make_csv(parser.MAX_ROWS)
    response = client.post("/validate", files=_csv_file("big.csv", data))
    assert response.status_code == 200
    html = response.text
    assert "10000 rows read: 0 errors, 0 warnings." in html
    assert "20 parts" in html
    assert re.search(r'<button type="submit" id="download" data-parts="20">', html)
    assert "Download 20 ZIP files" in html


def test_validate_single_part_file(client: TestClient, fixture_bytes) -> None:
    html = client.post(
        "/validate", files=_csv_file("v.csv", fixture_bytes("valid_minimal.csv"))
    ).text
    assert re.search(r'<button type="submit" id="download" data-parts="1">', html)
    assert "Download ZIP" in html and "parts" not in html.replace("data-parts", "")


@pytest.mark.parametrize(("rows", "parts"), [(500, 1), (501, 2), (1000, 2), (1001, 3)])
def test_validate_part_count(client: TestClient, rows: int, parts: int) -> None:
    html = client.post("/validate", files=_csv_file("v.csv", synthetic_csv(rows))).text
    assert f"{rows} rows read: 0 errors" in html
    assert f'data-parts="{parts}"' in html


def test_validate_too_many_rows(client: TestClient, too_many_rows_csv: bytes) -> None:
    response = client.post("/validate", files=_csv_file("big.csv", too_many_rows_csv))
    assert response.status_code == 200
    assert "Too many rows: the limit is 10000" in response.text
    assert 'id="download"' not in response.text


def test_validate_finds_duplicates_across_the_whole_file(client: TestClient) -> None:
    lines = synthetic_csv(parser.MAX_ROWS).decode().splitlines()
    lines[9000] = lines[1]  # row 9001 repeats row 2
    data = ("\n".join(lines) + "\n").encode()
    html = client.post("/validate", files=_csv_file("dup.csv", data)).text
    assert "10000 rows read: 2 errors, 0 warnings." in html
    assert (
        '<tr class="error"><td>9001</td><td>iccid</td><td>Duplicate ICCID; first seen in row 2.'
        in html
    )
    assert 'id="download"' not in html


def test_validate_caps_the_issue_table(client: TestClient) -> None:
    lines = synthetic_csv(700).decode().replace("LPA:1$", "NOPE$").splitlines()
    data = ("\n".join(lines) + "\n").encode()
    html = client.post("/validate", files=_csv_file("bad.csv", data)).text
    assert "700 rows read: 700 errors, 0 warnings." in html
    assert html.count('<tr class="error">') == routes.MAX_ISSUES_SHOWN
    assert f"Showing the first {routes.MAX_ISSUES_SHOWN} of 700" in html


@pytest.mark.parametrize(
    ("part", "rows", "suffix"),
    [
        (1, ["2", "3", "4"], "part01of03"),
        (2, ["5", "6", "7"], "part02of03"),
        (3, ["8", "9"], "part03of03"),
    ],
    ids=["first", "middle", "uneven-last"],
)
def test_generate_part_boundaries(
    client: TestClient, chunk_of_three, part: int, rows: list[str], suffix: str
) -> None:
    response = _post_part(client, synthetic_csv(8), part, 3)
    assert response.status_code == 200
    assert _zip_name(response) == f"esim_batch_16112026_0935_{suffix}.zip"
    manifest = _manifest(response)
    assert [row[0] for row in manifest[1:]] == rows
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == [*[row[2] for row in manifest[1:]], zipper.MANIFEST_NAME]


def test_generate_parts_cover_every_row_exactly_once(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 4)
    data = synthetic_csv(10)
    rows: list[str] = []
    pdfs: list[str] = []
    names: list[str] = []
    for part in (1, 2, 3):
        response = _post_part(client, data, part, 3)
        assert response.status_code == 200
        names.append(_zip_name(response))
        manifest = _manifest(response)
        rows += [row[0] for row in manifest[1:]]
        pdfs += [row[2] for row in manifest[1:]]
    assert rows == [str(n) for n in range(2, 12)]
    assert len(set(pdfs)) == 10
    assert names == [f"esim_batch_16112026_0935_part0{n}of03.zip" for n in (1, 2, 3)]


def test_generate_single_part_has_no_suffix(client: TestClient, fixture_bytes) -> None:
    data = fixture_bytes("valid_minimal.csv")
    assert _zip_name(_post_part(client, data, 1, 1)) == "esim_batch_16112026_0935.zip"
    plain = client.post(
        "/generate", data={**FORM, "client_stamp": "16112026_0935"}, files=_csv_file("v.csv", data)
    )
    assert _zip_name(plain) == "esim_batch_16112026_0935.zip"


def test_generate_part_mailmerge_covers_only_its_rows(client: TestClient, chunk_of_three) -> None:
    lines = ["iccid,activation_code,email"] + [
        f"{synthetic_iccid(i)},LPA:1$smdp.example.invalid$TEST-{i:04d}-AAAA-BBBB,u{i}@example.invalid"
        for i in range(1, 8)
    ]
    response = _post_part(client, ("\n".join(lines) + "\n").encode(), 2, 3)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        rows = list(csv.reader(io.StringIO(zf.read(zipper.MAILMERGE_NAME).decode("utf-8-sig"))))
    assert [row[0] for row in rows[1:]] == [f"u{i}@example.invalid" for i in (4, 5, 6)]


@pytest.mark.parametrize(
    "part", [0, 4, -1, 99, "abc", "1.5", "", " 2", "2 ", "+2", "0x2", "²", "1e0"]
)
def test_generate_part_index_out_of_range_is_400(client: TestClient, chunk_of_three, part) -> None:
    response = _post_part(client, synthetic_csv(8), part, 3)
    assert response.status_code == 400
    assert routes.MSG_BAD_PART in response.text
    assert "<html" in response.text


@pytest.mark.parametrize("parts", [1, 2, 4, 20, 0, -3, "abc", "", "3.0"])
def test_generate_part_count_mismatch_is_400(client: TestClient, chunk_of_three, parts) -> None:
    response = _post_part(client, synthetic_csv(8), 1, parts)
    assert response.status_code == 400
    assert routes.MSG_BAD_PART in response.text


def test_generate_multi_part_file_without_part_fields_is_400(
    client: TestClient, chunk_of_three
) -> None:
    response = client.post("/generate", data=FORM, files=_csv_file("v.csv", synthetic_csv(8)))
    assert response.status_code == 400
    assert routes.MSG_BAD_PART in response.text


def test_generate_revalidates_the_whole_file_for_each_part(
    client: TestClient, chunk_of_three
) -> None:
    lines = synthetic_csv(8).decode().splitlines()
    lines[7] = lines[1]  # row 8 (part 3) repeats row 2 (part 1)
    data = ("\n".join(lines) + "\n").encode()
    for part in (1, 2, 3):
        response = _post_part(client, data, part, 3)
        assert response.status_code == 422
        assert '<tr class="error"><td>8</td><td>iccid</td>' in response.text


def test_generate_real_chunk_size(client: TestClient) -> None:
    data = synthetic_csv(1001)
    last = _post_part(client, data, 3, 3)
    assert last.status_code == 200
    assert _zip_name(last) == "esim_batch_16112026_0935_part03of03.zip"
    assert [row[0] for row in _manifest(last)[1:]] == ["1002"]
    assert _post_part(client, data, 4, 3).status_code == 400
    assert _post_part(client, data, 1, 2).status_code == 400


def test_generate_rejects_bad_part_before_taking_a_job_slot(
    client: TestClient, monkeypatch
) -> None:
    def never(*args, **kwargs):
        raise AssertionError("pipeline must not run for a malformed part number")

    monkeypatch.setattr(routes, "process_upload", never)
    assert _post_part(client, synthetic_csv(3), "abc", 1).status_code == 400

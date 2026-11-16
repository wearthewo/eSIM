from __future__ import annotations

import csv
import io
import zipfile

import pytest

from app.core import zipper

PDF = b"%PDF-1.4 synthetic"


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


# --- sanitize_filename --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("esim_8999000000000000013.pdf", "esim_8999000000000000013.pdf"),
        ("../x.pdf", "x.pdf"),
        ("..\\x.pdf", "x.pdf"),
        ("C:\\x.pdf", "x.pdf"),
        ("C:x.pdf", "Cx.pdf"),
        ("/etc/passwd", "passwd"),
        ("a/../../b.pdf", "b.pdf"),
        ("a..b.pdf", "ab.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("ev\x00il\x1f\x7f.pdf", "evil.pdf"),
        ("bidi\u202etxt.pdf", "biditxt.pdf"),
        ("Γιώργος Παπαδόπουλος.pdf", "Γιώργος Παπαδόπουλος.pdf"),
        ("name.PDF", "name.PDF"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert zipper.sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "file"),
        ("..", "file"),
        ("../..", "file"),
        ("/", "file"),
        ("\x00\x01", "file"),
        ("../.pdf", "file.pdf"),
        ("\x00.pdf", "file.pdf"),
        ("...", "file"),
        ("...pdf", "file.pdf"),
    ],
)
def test_sanitize_filename_falls_back_and_keeps_extension(raw: str, expected: str) -> None:
    assert zipper.sanitize_filename(raw) == expected


def test_sanitize_filename_never_returns_unsafe_name() -> None:
    for raw in ["../x.pdf", "C:\\x.pdf", "..\\..\\..\\windows\\x.pdf", "a/./b", " ..", "x:y"]:
        out = zipper.sanitize_filename(raw)
        assert out
        assert "/" not in out and "\\" not in out and ":" not in out
        assert ".." not in out
        assert not out.startswith(".")


# --- escape_csv_cell ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+1+1", "-2+3", "@SUM(A1)", "\tTabbed", "\rCR", '=HYPERLINK("http://x.invalid")'],
)
def test_escape_csv_cell_prefixes_dangerous_cells(value: str) -> None:
    assert zipper.escape_csv_cell(value) == "'" + value


@pytest.mark.parametrize(
    "value", ["", "8999000000000000013", "Test Customer", "a=b", "Γιώργος", "'quoted"]
)
def test_escape_csv_cell_leaves_safe_cells(value: str) -> None:
    assert zipper.escape_csv_cell(value) == value


# --- build_manifest -----------------------------------------------------------------


def test_build_manifest() -> None:
    data = zipper.build_manifest(
        [
            (2, "8999000000000000013", "esim_8999000000000000013.pdf"),
            (3, "8999000000000000021", "esim_8999000000000000021.pdf"),
        ]
    )
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows == [
        ["row_number", "iccid", "pdf_filename"],
        ["2", "8999000000000000013", "esim_8999000000000000013.pdf"],
        ["3", "8999000000000000021", "esim_8999000000000000021.pdf"],
    ]


def test_build_manifest_empty_has_header_only() -> None:
    rows = list(csv.reader(io.StringIO(zipper.build_manifest([]).decode("utf-8-sig"))))
    assert rows == [list(zipper.MANIFEST_COLUMNS)]


def test_build_manifest_escapes_every_cell() -> None:
    data = zipper.build_manifest([(2, "=cmd|'/c calc'!A1", "@evil.pdf"), (3, "+1", "-x.pdf")])
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[1] == ["2", "'=cmd|'/c calc'!A1", "'@evil.pdf"]
    assert rows[2] == ["3", "'+1", "'-x.pdf"]


def test_build_manifest_greek_round_trips() -> None:
    name = "Γιώργος_Παπαδόπουλος.pdf"
    data = zipper.build_manifest([(2, "8999000000000000500", name)])
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[1][2] == name


# --- build_zip ----------------------------------------------------------------------


def test_build_zip() -> None:
    data = zipper.build_zip([("a.pdf", PDF), ("b.pdf", b"%PDF-other")])
    with _open(data) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["a.pdf", "b.pdf"]
        assert zf.read("a.pdf") == PDF
        assert zf.read("b.pdf") == b"%PDF-other"
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())


def test_build_zip_adds_manifest() -> None:
    manifest = zipper.build_manifest([(2, "8999000000000000013", "a.pdf")])
    data = zipper.build_zip([("a.pdf", PDF)], manifest)
    with _open(data) as zf:
        assert zf.namelist() == ["a.pdf", zipper.MANIFEST_NAME]
        assert zf.read(zipper.MANIFEST_NAME) == manifest
        assert zf.getinfo(zipper.MANIFEST_NAME).compress_type == zipfile.ZIP_STORED


def test_build_zip_without_manifest_has_no_manifest() -> None:
    with _open(zipper.build_zip([("a.pdf", PDF)])) as zf:
        assert zipper.MANIFEST_NAME not in zf.namelist()


def test_build_zip_empty() -> None:
    with _open(zipper.build_zip([])) as zf:
        assert zf.namelist() == []


def test_build_zip_sanitizes_traversal_names() -> None:
    data = zipper.build_zip([("../x.pdf", PDF), ("C:\\y.pdf", PDF), ("/abs/z.pdf", PDF)])
    with _open(data) as zf:
        assert zf.namelist() == ["x.pdf", "y.pdf", "z.pdf"]
        for name in zf.namelist():
            assert "/" not in name and "\\" not in name and ".." not in name


@pytest.mark.parametrize(
    "files",
    [
        [("a.pdf", PDF), ("a.pdf", PDF)],
        [("x.pdf", PDF), ("../x.pdf", PDF)],
        [("x.pdf", PDF), ("C:\\x.pdf", PDF)],
        [("A.pdf", PDF), ("a.pdf", PDF)],
    ],
)
def test_build_zip_rejects_duplicates_after_sanitizing(files: list[tuple[str, bytes]]) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        zipper.build_zip(files)


def test_build_zip_rejects_file_named_like_manifest() -> None:
    manifest = zipper.build_manifest([])
    with pytest.raises(ValueError, match="Duplicate"):
        zipper.build_zip([("manifest.csv", b"x")], manifest)


def test_build_zip_greek_filenames_round_trip() -> None:
    name = "Γιώργος Παπαδόπουλος.pdf"
    data = zipper.build_zip([(name, PDF)])
    with _open(data) as zf:
        info = zf.infolist()[0]
        assert info.filename == name
        assert info.flag_bits & 0x800  # UTF-8 filename flag
        assert zf.read(name) == PDF


def test_build_zip_is_deterministic() -> None:
    files = [("a.pdf", PDF), ("Ελένη.pdf", b"%PDF-2")]
    manifest = zipper.build_manifest([(2, "8999000000000000013", "a.pdf")])
    first = zipper.build_zip(files, manifest)
    second = zipper.build_zip(files, manifest)
    assert first == second
    with _open(first) as zf:
        assert {i.date_time for i in zf.infolist()} == {zipper.FIXED_DATE_TIME}

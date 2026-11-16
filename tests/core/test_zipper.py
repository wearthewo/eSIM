from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet import _writer as worksheet_writer

from app.core import sheets, zipper
from tests.conftest import read_xlsx

PDF = b"%PDF-1.4 synthetic"


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _sheet_xml(data: bytes) -> bytes:
    with _open(data) as zf:
        return zf.read("xl/worksheets/sheet1.xml")


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


# --- build_manifest (manifest.xlsx) -------------------------------------------------


def test_build_manifest() -> None:
    data = zipper.build_manifest(
        [
            (2, "8999000000000000013", "esim_8999000000000000013.pdf"),
            (3, "89990000000000000210", "esim_89990000000000000210.pdf"),
        ]
    )
    assert data.startswith(b"PK\x03\x04")  # an XLSX package, not CSV
    rows = read_xlsx(data)  # also asserts every cell is a text-formatted string cell
    assert rows == [
        ["row_number", "iccid", "pdf_filename"],
        ["2", "8999000000000000013", "esim_8999000000000000013.pdf"],
        ["3", "89990000000000000210", "esim_89990000000000000210.pdf"],
    ]
    assert [len(row[1]) for row in rows[1:]] == [19, 20]  # every digit kept


def test_build_manifest_empty_has_header_only() -> None:
    assert read_xlsx(zipper.build_manifest([])) == [list(zipper.MANIFEST_COLUMNS)]


@pytest.mark.parametrize(
    "value",
    [
        "=cmd|'/c calc'!A1",
        '=HYPERLINK("http://x.invalid")',
        "+1+1",
        "-2+3",
        "@SUM(A1)",
        "=1",
        "#N/A",
        "\tTabbed",
    ],
)
def test_build_manifest_formula_like_cells_stay_literal_text(value: str) -> None:
    data = zipper.build_manifest([(2, value, value + ".pdf")])
    assert read_xlsx(data)[1] == ["2", value, value + ".pdf"]  # unchanged, no ' prefix
    sheet = _sheet_xml(data)
    assert b"<f>" not in sheet and b"<f " not in sheet  # no formula elements
    assert b't="e"' not in sheet  # no error cells either


def test_build_manifest_greek_round_trips() -> None:
    name = "Γιώργος_Παπαδόπουλος.pdf"
    data = zipper.build_manifest([(2, "8999000000000000500", name)])
    assert read_xlsx(data)[1][2] == name


def test_build_manifest_format() -> None:
    data = zipper.build_manifest([(2, "8999000000000000013", "esim_8999000000000000013.pdf")])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws.freeze_panes == "A2"
    assert all(cell.font.b for cell in ws[1])
    assert not any(cell.font.b for cell in ws[2])
    for letter, minimum in (("A", 10), ("B", 19), ("C", 28)):
        column = ws.column_dimensions[letter]
        assert column.number_format == "@"  # new cells typed into the column stay text
        assert minimum <= column.width <= sheets.MAX_COLUMN_WIDTH


def test_build_manifest_is_deterministic_with_fixed_timestamps() -> None:
    # openpyxl's Workbook.save would stamp "now" into docProps/core.xml and into
    # every entry of the XLSX archive; both must be fixed.
    entries = [(2, "8999000000000000013", "a.pdf")]
    first = zipper.build_manifest(entries)
    assert first == zipper.build_manifest(entries)
    with _open(first) as zf:
        assert {i.date_time for i in zf.infolist()} == {zipper.FIXED_DATE_TIME}
        core = zf.read("docProps/core.xml")
    assert core.count(b"1980-01-01T00:00:00Z") == 2  # created and modified
    props = load_workbook(io.BytesIO(first)).properties
    assert props.created == props.modified == sheets.FIXED_TIMESTAMP


def test_build_manifest_never_touches_disk(monkeypatch) -> None:
    def no_temp_files(*args, **kwargs):
        raise AssertionError("openpyxl tried to stage the sheet in a temporary file")

    monkeypatch.setattr(worksheet_writer, "create_temporary_file", no_temp_files)
    assert read_xlsx(zipper.build_manifest([(2, "8999000000000000013", "a.pdf")]))[1][0] == "2"


def test_build_manifest_drops_xml_illegal_control_characters() -> None:
    # XML 1.0 cannot store these; tab, CR and LF are kept.
    data = zipper.build_manifest([(2, "8999000000000000013", "a\x00b\x01c\x1fd\te\nf.pdf")])
    assert read_xlsx(data)[1][2] == "abcd\te\nf.pdf"


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
        zipper.build_zip([("manifest.xlsx", b"x")], manifest)


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

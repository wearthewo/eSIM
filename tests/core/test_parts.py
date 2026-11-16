"""Large orders: the 10,000-row limit and chunked generation (one ZIP per part).

Every part request carries the whole file. The whole file is validated each
time (so duplicates are caught across parts), then only that part's rows are
rendered. Most tests shrink ``GENERATE_CHUNK_ROWS`` so they render few PDFs.
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from app.core import parser, pipeline, validator, zipper
from app.core.models import Branding, ParseError
from tests.conftest import synthetic_csv, synthetic_iccid

BRANDING = Branding(company_name="Example Telecom (test)")


def _csv_with_emails(count: int) -> bytes:
    lines = ["iccid,activation_code,customer_name,email"]
    lines += [
        f"{synthetic_iccid(i)},LPA:1$smdp.example.invalid$TEST-{i:05d}-AAAA-BBBB,"
        f"Customer {i},user{i}@example.invalid"
        for i in range(1, count + 1)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _rows(zf: zipfile.ZipFile, name: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(zf.read(name).decode("utf-8-sig"))))


def _part(data: bytes, part: int, parts: int) -> zipfile.ZipFile:
    report, archive = pipeline.process_upload(data, "x.csv", BRANDING, part=part, parts=parts)
    assert report.ok and archive is not None
    return zipfile.ZipFile(io.BytesIO(archive))


# --- limits ---------------------------------------------------------------------------


def test_limits() -> None:
    assert parser.MAX_ROWS == 10_000
    assert pipeline.GENERATE_CHUNK_ROWS == 500


@pytest.mark.parametrize(
    ("rows", "expected"),
    [(0, 1), (1, 1), (499, 1), (500, 1), (501, 2), (1000, 2), (1001, 3), (9501, 20), (10_000, 20)],
)
def test_part_count(rows: int, expected: int) -> None:
    assert pipeline.part_count(rows) == expected


def test_ten_thousand_rows_parse_and_validate() -> None:
    parsed, report = pipeline.check_upload(synthetic_csv(parser.MAX_ROWS), "big.csv")
    assert len(parsed.records) == parser.MAX_ROWS
    assert report.ok and len(report.valid) == parser.MAX_ROWS
    assert pipeline.part_count(len(parsed.records)) == 20


def test_ten_thousand_and_one_rows_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="10000"):
        pipeline.check_upload(synthetic_csv(parser.MAX_ROWS + 1), "big.csv")


def test_duplicates_are_found_across_the_whole_file() -> None:
    lines = synthetic_csv(parser.MAX_ROWS).decode().splitlines()
    lines[9000] = lines[1]  # row 9001 repeats row 2: parts 1 and 18
    report = validator.validate(parser.parse_csv(("\n".join(lines) + "\n").encode()))
    assert {(i.row_number, i.code) for i in report.errors} == {
        (9001, "duplicate_iccid"),
        (9001, "duplicate_activation_code"),
    }
    assert "row 2" in report.errors[0].message


# --- part boundaries --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("part", "expected_rows"),
    [(1, [2, 3, 4]), (2, [5, 6, 7]), (3, [8, 9])],
    ids=["first", "middle", "uneven-last"],
)
def test_each_part_holds_only_its_rows(monkeypatch, part: int, expected_rows: list[int]) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with _part(_csv_with_emails(8), part, 3) as zf:
        iccids = [synthetic_iccid(row - 1) for row in expected_rows]
        pdfs = [f"esim_{iccid}.pdf" for iccid in iccids]
        assert zf.namelist() == [*pdfs, zipper.MANIFEST_NAME, zipper.MAILMERGE_NAME]
        assert _rows(zf, zipper.MANIFEST_NAME) == [
            list(zipper.MANIFEST_COLUMNS),
            *[[str(r), i, p] for r, i, p in zip(expected_rows, iccids, pdfs, strict=True)],
        ]
        assert _rows(zf, zipper.MAILMERGE_NAME) == [
            list(zipper.MAILMERGE_COLUMNS),
            *[
                [f"user{r - 1}@example.invalid", f"Customer {r - 1}", "", i, p]
                for r, i, p in zip(expected_rows, iccids, pdfs, strict=True)
            ],
        ]


def test_even_last_part(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with _part(synthetic_csv(6), 2, 2) as zf:
        assert [row[0] for row in _rows(zf, zipper.MANIFEST_NAME)[1:]] == ["5", "6", "7"]


def test_no_row_is_in_two_parts_and_none_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 4)
    data = synthetic_csv(10)
    seen_rows: list[str] = []
    seen_pdfs: list[str] = []
    for part in (1, 2, 3):
        with _part(data, part, 3) as zf:
            seen_rows += [row[0] for row in _rows(zf, zipper.MANIFEST_NAME)[1:]]
            seen_pdfs += [n for n in zf.namelist() if n.endswith(".pdf")]
    assert seen_rows == [str(r) for r in range(2, 12)]
    assert len(set(seen_pdfs)) == len(seen_pdfs) == 10


def test_parts_are_deterministic_and_independent(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    data = synthetic_csv(7)
    first = pipeline.process_upload(data, "x.csv", BRANDING, part=2, parts=3)[1]
    pipeline.process_upload(data, "x.csv", BRANDING, part=1, parts=3)
    assert first == pipeline.process_upload(data, "x.csv", BRANDING, part=2, parts=3)[1]


def test_real_chunk_size_last_part_of_1001_rows() -> None:
    with _part(synthetic_csv(1001), 3, 3) as zf:
        assert zf.namelist() == [f"esim_{synthetic_iccid(1001)}.pdf", zipper.MANIFEST_NAME]
        assert _rows(zf, zipper.MANIFEST_NAME)[1][0] == "1002"


def test_single_part_is_the_default(fixture_bytes) -> None:
    data = fixture_bytes("valid_minimal.csv")
    default = pipeline.process_upload(data, "v.csv", BRANDING)
    assert default == pipeline.process_upload(data, "v.csv", BRANDING, part=1, parts=1)
    assert default[1] is not None


# --- bad part arguments -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("part", "parts"),
    [(0, 3), (4, 3), (-1, 3), (1, 0), (1, -3)],
    ids=["zero", "past-the-end", "negative", "no-parts", "negative-parts"],
)
def test_part_index_out_of_range(monkeypatch, part: int, parts: int) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with pytest.raises(pipeline.PartError):
        pipeline.process_upload(synthetic_csv(8), "x.csv", BRANDING, part=part, parts=parts)


@pytest.mark.parametrize("parts", [1, 2, 4, 20])
def test_part_count_must_match_the_file(monkeypatch, parts: int) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with pytest.raises(pipeline.PartError):
        pipeline.process_upload(synthetic_csv(8), "x.csv", BRANDING, part=1, parts=parts)


def test_multi_part_file_without_part_arguments_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with pytest.raises(pipeline.PartError):
        pipeline.process_upload(synthetic_csv(8), "x.csv", BRANDING)


def test_part_error_message_has_no_cell_values(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    with pytest.raises(pipeline.PartError) as exc:
        pipeline.process_upload(synthetic_csv(8), "x.csv", BRANDING, part=1, parts=2)
    for secret in ("LPA:1$", "TEST-", "smdp.example.invalid", "8999"):
        assert secret not in str(exc.value)


# --- the whole file is validated for every part ------------------------------------------------


def test_error_in_another_part_blocks_every_part(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    lines = synthetic_csv(8).decode().splitlines()
    lines[8] = lines[8].replace("LPA:1$", "NOPE$")  # row 9, in part 3
    data = ("\n".join(lines) + "\n").encode()
    for part in (1, 2, 3):
        report, archive = pipeline.process_upload(data, "x.csv", BRANDING, part=part, parts=3)
        assert archive is None
        assert [(i.row_number, i.field) for i in report.errors] == [(9, "activation_code")]


def test_duplicate_across_parts_blocks_every_part(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    lines = synthetic_csv(8).decode().splitlines()
    lines[7] = lines[1]  # row 8 (part 3) repeats row 2 (part 1)
    data = ("\n".join(lines) + "\n").encode()
    for part in (1, 3):
        report, archive = pipeline.process_upload(data, "x.csv", BRANDING, part=part, parts=3)
        assert archive is None
        assert {i.code for i in report.errors} == {"duplicate_iccid", "duplicate_activation_code"}


def test_only_the_requested_part_is_rendered(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    rendered: list[int] = []
    original = pipeline.render_pdf

    def counting(record, branding):
        rendered.append(record.row_number)
        return original(record, branding)

    monkeypatch.setattr(pipeline, "render_pdf", counting)
    pipeline.process_upload(synthetic_csv(8), "x.csv", BRANDING, part=2, parts=3)
    assert rendered == [5, 6, 7]

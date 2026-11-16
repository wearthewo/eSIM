from __future__ import annotations

import csv
import io
import zipfile

import pytest

from app.core import pipeline, render, zipper
from app.core.models import Branding, ParseError
from tests.conftest import synthetic_csv, synthetic_iccid

BRANDING = Branding(company_name="Example Telecom (test)", footer_text="help@example.invalid")


def _manifest_rows(zf: zipfile.ZipFile) -> list[list[str]]:
    text = zf.read(zipper.MANIFEST_NAME).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _codes(report) -> set[tuple[int, str, str]]:
    return {(i.row_number, i.code, i.severity) for i in report.issues}


def test_process_upload(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("valid_minimal.csv"), "valid_minimal.csv", BRANDING
    )
    assert report.ok and not report.issues
    assert data is not None
    iccids = ["8999000000000000013", "8999000000000000021", "8999000000000000039"]
    pdfs = [f"esim_{iccid}.pdf" for iccid in iccids]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [*pdfs, zipper.MANIFEST_NAME]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        for name in pdfs:
            assert zf.read(name).startswith(b"%PDF-")
        assert _manifest_rows(zf) == [
            ["row_number", "iccid", "pdf_filename"],
            *[
                [str(row), iccid, pdf]
                for row, iccid, pdf in zip((2, 3, 4), iccids, pdfs, strict=True)
            ],
        ]


def test_process_upload_valid_full_manifest_has_no_secrets(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("valid_full.csv"), "valid_full.csv", BRANDING
    )
    assert report.ok
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        pdfs = [n for n in zf.namelist() if n.endswith(".pdf")]
        assert len(pdfs) == len(report.valid) == 5
        manifest = zf.read(zipper.MANIFEST_NAME)
    text = manifest.decode("utf-8-sig")
    for n in range(10, 15):
        assert f"TEST-{n:04d}-AAAA-BBBB" not in text  # matching ID
        assert f"CONF{n:04d}" not in text  # confirmation code
    assert "LPA:" not in text and "smdp.example.invalid" not in text
    for record in report.valid:
        assert record.activation_code.encode() not in manifest


def test_process_upload_is_deterministic(fixture_bytes) -> None:
    data = fixture_bytes("valid_minimal.csv")
    first = pipeline.process_upload(data, "valid_minimal.csv", BRANDING)[1]
    second = pipeline.process_upload(data, "valid_minimal.csv", BRANDING)[1]
    assert first == second


def test_process_upload_mixed_valid_invalid_gives_no_zip(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("mixed_valid_invalid.csv"), "mixed_valid_invalid.csv", BRANDING
    )
    assert data is None
    assert not report.ok
    assert _codes(report) == {
        (3, "iccid_luhn", "error"),
        (5, "activation_code_prefix", "error"),
    }
    assert [r.row_number for r in report.valid] == [2, 4]


def test_process_upload_warning_only_gives_zip(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("warning_iccid_prefix.csv"), "warning_iccid_prefix.csv", BRANDING
    )
    assert report.ok
    assert _codes(report) == {(2, "iccid_prefix", "warning")}
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["esim_9999000000000000608.pdf", zipper.MANIFEST_NAME]


def test_process_upload_duplicates_gives_no_zip(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("duplicates.csv"), "duplicates.csv", BRANDING
    )
    assert data is None
    assert _codes(report) == {
        (3, "duplicate_iccid", "error"),
        (4, "duplicate_activation_code", "error"),
    }


def test_process_upload_greek_names(fixture_bytes) -> None:
    report, data = pipeline.process_upload(
        fixture_bytes("valid_greek_names.csv"), "valid_greek_names.csv", BRANDING
    )
    assert report.ok and data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.namelist()) == 4


def test_process_upload_parse_error_propagates(fixture_bytes) -> None:
    with pytest.raises(ParseError):
        pipeline.process_upload(fixture_bytes("unsupported.txt"), "unsupported.txt", BRANDING)
    with pytest.raises(ParseError):
        pipeline.process_upload(
            fixture_bytes("missing_activation_columns.csv"),
            "missing_activation_columns.csv",
            BRANDING,
        )


def test_process_upload_registers_fonts_once(fixture_bytes, monkeypatch) -> None:
    calls: list[None] = []
    original = render.register_fonts

    def counting() -> None:
        calls.append(None)
        original()

    monkeypatch.setattr(pipeline, "register_fonts", counting)
    monkeypatch.setattr(pipeline, "_fonts_ready", False)
    for _ in range(2):
        pipeline.process_upload(fixture_bytes("valid_full.csv"), "valid_full.csv", BRANDING)
    assert len(calls) == 1


def test_process_upload_500_rows() -> None:
    report, data = pipeline.process_upload(synthetic_csv(500), "bulk.csv", BRANDING)
    assert report.ok
    assert data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert len(names) == 501
        assert names[-1] == zipper.MANIFEST_NAME
        assert names[0] == f"esim_{synthetic_iccid(1)}.pdf"
        assert len(_manifest_rows(zf)) == 501

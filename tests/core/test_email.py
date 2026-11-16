"""The optional ``email`` column and ``mailmerge.csv`` (parser, validator, models, zip, pipeline).

The server never sends mail. The address only goes into ``mailmerge.csv`` so the
user can run a mail merge. It is never printed on a PDF and never logged.
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from app.core import parser, pipeline, render, validator, zipper
from app.core.models import Branding, EsimRecord
from tests.conftest import FIXTURES_DIR, build_xlsx
from tests.core.test_render import decompressed, extract_text

AC1 = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"
AC2 = "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB"
ICCID1 = "8999000000000000013"
ICCID2 = "8999000000000000021"
BRANDING = Branding(company_name="Example Telecom (test)", footer_text="help@example.invalid")
FIXTURE = "emails_greek_mixed.csv"
MISSING_EMAIL_MESSAGE = "No email; row will have an empty address in mailmerge.csv"
SECRET_MARKERS = ("LPA:1$", "smdp.example.invalid", "TEST-0", "CONF0")


def _rec(row: int = 2, iccid: str = ICCID1, activation_code: str = AC1, **kwargs) -> EsimRecord:
    return EsimRecord(row_number=row, iccid=iccid, activation_code=activation_code, **kwargs)


def _valid_fixture() -> bytes:
    """The fixture without its one invalid-email row (row 4), so it validates."""
    lines = (FIXTURES_DIR / FIXTURE).read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.endswith(",eleni.example.invalid")]
    assert len(kept) == len(lines) - 1
    return ("\n".join(kept) + "\n").encode("utf-8")


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


# --- parser ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["email", "Email", "EMAIL", "  eMail  "])
def test_parser_email_header_is_case_insensitive(header: str) -> None:
    parsed = parser.parse_table(
        f"iccid,activation_code,{header}\n{ICCID1},{AC1},a@example.invalid\n".encode(), "x.csv"
    )
    assert parsed.has_email
    assert parsed.records[0].email == "a@example.invalid"


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("  maria@example.invalid  ", "maria@example.invalid"),
        ("Maria.P@Example.INVALID", "Maria.P@example.invalid"),  # local part keeps its case
        ("A@B@EXAMPLE.Invalid", "A@B@example.invalid"),  # split at the last @; validator rejects
        ("NoAtSign.Example", "NoAtSign.Example"),  # left alone; validator rejects
        ("", None),
        ("   ", None),
    ],
)
def test_parser_email_is_trimmed_and_domain_lowercased(cell: str, expected: str | None) -> None:
    data = f'iccid,activation_code,email\n{ICCID1},{AC1},"{cell}"\n'.encode()
    assert parser.parse_csv(data)[0].email == expected


def test_parser_without_email_column() -> None:
    parsed = parser.parse_table(f"iccid,activation_code\n{ICCID1},{AC1}\n".encode(), "x.csv")
    assert not parsed.has_email
    assert parsed.records[0].email is None
    assert parsed.columns == frozenset({"iccid", "activation_code"})


def test_parser_email_column_with_empty_cell_still_counts_as_present() -> None:
    parsed = parser.parse_table(f"iccid,activation_code,email\n{ICCID1},{AC1},\n".encode(), "x.csv")
    assert parsed.has_email
    assert parsed.records[0].email is None


def test_parser_email_xlsx_matches_csv() -> None:
    rows = [
        ["ICCID", "Activation_Code", "E-Mail ignored", "Email"],
        [ICCID1, AC1, "x", " Maria.P@Example.INVALID "],
        [ICCID2, AC2, "x", None],
    ]
    parsed = parser.parse_table(build_xlsx(rows), "x.xlsx")
    assert parsed.has_email
    assert [r.email for r in parsed.records] == ["Maria.P@example.invalid", None]
    text = "iccid,activation_code,email\n" + "".join(
        f"{r[0]},{r[1]},{r[3] or ''}\n" for r in rows[1:]
    )
    assert parser.parse_table(text.encode(), "x.csv").records == parsed.records


def test_parse_upload_still_returns_records(fixture_bytes) -> None:
    records = parser.parse_upload(fixture_bytes(FIXTURE), FIXTURE)
    assert [r.email for r in records] == [
        "maria.p@example.invalid",
        "Giorgos.N@mail.example.invalid",
        "eleni.example.invalid",
        None,
        "maria.p@example.invalid",
    ]
    assert records[0].customer_name == "Μαρία Παπαδοπούλου"


# --- validator ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "a@example.invalid",
        "first.last+tag@mail.example.invalid",
        "o'brien@example.invalid",
        "μαρία@παράδειγμα.invalid",
        "x" * (254 - len("@example.invalid")) + "@example.invalid",  # exactly 254
    ],
)
def test_validate_email_accepts(email: str) -> None:
    assert validator.validate_email(email) is None


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign.example.invalid",
        "two@@example.invalid",
        "a@b@example.invalid",
        "@example.invalid",  # empty local part
        "a@nodot",
        "a@.invalid",
        "a@example.",
        "a@example..invalid",
        "a b@example.invalid",
        "a@exam ple.invalid",
        "a\t@example.invalid",
        "a@example.invalid\n",
        "x" * (255 - len("@example.invalid")) + "@example.invalid",  # 255
    ],
)
def test_validate_email_rejects(email: str) -> None:
    assert validator.validate_email(email) == "email_format"


def test_invalid_email_is_an_error_that_never_echoes_the_value() -> None:
    bad = "not-an-email.example"
    report = validator.validate([_rec(email=bad)], has_email_column=True)
    assert not report.ok and report.valid == []
    (issue,) = report.issues
    assert (issue.row_number, issue.field, issue.code, issue.severity) == (
        2,
        "email",
        "email_format",
        "error",
    )
    assert bad not in issue.message


def test_empty_email_is_a_warning_only_when_the_column_exists() -> None:
    records = [_rec(email=None)]
    report = validator.validate(records, has_email_column=True)
    assert report.ok and report.valid == records
    (issue,) = report.issues
    assert (issue.field, issue.code, issue.severity) == ("email", "email_missing", "warning")
    assert issue.message == MISSING_EMAIL_MESSAGE

    assert validator.validate(records).issues == []
    assert validator.validate(records, has_email_column=False).issues == []


def test_duplicate_emails_are_allowed() -> None:
    records = [
        _rec(2, ICCID1, AC1, email="same@example.invalid"),
        _rec(3, ICCID2, AC2, email="same@example.invalid"),
    ]
    report = validator.validate(records, has_email_column=True)
    assert report.issues == [] and report.valid == records


def test_fixture_reports_one_error_and_one_warning(fixture_bytes) -> None:
    parsed = parser.parse_table(fixture_bytes(FIXTURE), FIXTURE)
    report = validator.validate(parsed.records, has_email_column=parsed.has_email)
    assert [(i.row_number, i.field, i.code, i.severity) for i in report.issues] == [
        (4, "email", "email_format", "error"),
        (5, "email", "email_missing", "warning"),
    ]
    assert [r.row_number for r in report.valid] == [2, 3, 5, 6]


# --- models -----------------------------------------------------------------------------


def test_repr_redacts_email() -> None:
    text = repr(_rec(email="maria.p@example.invalid", customer_name="Test Customer"))
    assert "maria.p" not in text and "example.invalid" not in text
    assert "email='<redacted>'" in text
    assert "customer_name='Test Customer'" in text
    assert "email=None" in repr(_rec())


# --- render: never on the PDF -----------------------------------------------------------


def test_email_is_never_printed_on_the_pdf() -> None:
    email = "maria.p@mailmerge.example"
    record = _rec(email=email, customer_name="Μαρία Παπαδοπούλου", plan_name="Ταξίδι 10GB")
    data = render.render_pdf(record, BRANDING)
    raw = data + decompressed(data)
    text = extract_text(data)
    for needle in (email, "maria.p", "mailmerge.example"):
        assert needle not in text
        assert needle.encode() not in raw
    assert data == render.render_pdf(
        _rec(customer_name=record.customer_name, plan_name=record.plan_name), BRANDING
    )


# --- zipper: mailmerge.csv ----------------------------------------------------------------


def test_build_mailmerge() -> None:
    data = zipper.build_mailmerge(
        [
            (
                "maria@example.invalid",
                "Μαρία Παπαδοπούλου",
                "Ταξίδι 10GB",
                ICCID1,
                f"esim_{ICCID1}.pdf",
            ),
            ("", "", "", ICCID2, f"esim_{ICCID2}.pdf"),
        ]
    )
    assert data.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads the Greek as UTF-8
    assert _csv_rows(data) == [
        ["email", "customer_name", "plan_name", "iccid", "pdf_filename"],
        [
            "maria@example.invalid",
            "Μαρία Παπαδοπούλου",
            "Ταξίδι 10GB",
            ICCID1,
            f"esim_{ICCID1}.pdf",
        ],
        ["", "", "", ICCID2, f"esim_{ICCID2}.pdf"],
    ]
    assert zipper.MAILMERGE_COLUMNS == (
        "email",
        "customer_name",
        "plan_name",
        "iccid",
        "pdf_filename",
    )


def test_build_mailmerge_escapes_every_cell() -> None:
    data = zipper.build_mailmerge(
        [("=cmd|'/c calc'!A1@example.invalid", "+SUM(A1)", "-2+3", "@x", "\tname.pdf")]
    )
    assert _csv_rows(data)[1] == [
        "'=cmd|'/c calc'!A1@example.invalid",
        "'+SUM(A1)",
        "'-2+3",
        "'@x",
        "'\tname.pdf",
    ]


def test_build_mailmerge_quotes_commas_and_newlines() -> None:
    data = zipper.build_mailmerge(
        [("a@example.invalid", 'Last, "First"', "Line1\nLine2", ICCID1, "f.pdf")]
    )
    assert _csv_rows(data)[1][1:3] == ['Last, "First"', "Line1\nLine2"]


def test_build_zip_adds_mailmerge_after_manifest() -> None:
    manifest = zipper.build_manifest([(2, ICCID1, "a.pdf")])
    mailmerge = zipper.build_mailmerge([("a@example.invalid", "", "", ICCID1, "a.pdf")])
    data = zipper.build_zip([("a.pdf", b"%PDF-1.4 synthetic")], manifest, mailmerge)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["a.pdf", zipper.MANIFEST_NAME, zipper.MAILMERGE_NAME]
        assert zf.read(zipper.MAILMERGE_NAME) == mailmerge
    assert zipper.MAILMERGE_NAME == "mailmerge.csv"
    with pytest.raises(ValueError, match="Duplicate"):
        zipper.build_zip([("MailMerge.csv", b"x")], manifest, mailmerge)


# --- pipeline -----------------------------------------------------------------------------


def test_pipeline_adds_mailmerge_when_the_file_has_an_email_column() -> None:
    report, data = pipeline.process_upload(_valid_fixture(), "emails.csv", BRANDING)
    assert report.ok and data is not None
    assert [(i.row_number, i.code, i.severity) for i in report.issues] == [
        (4, "email_missing", "warning")
    ]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        pdfs = [n for n in zf.namelist() if n.endswith(".pdf")]
        assert zf.namelist() == [*pdfs, zipper.MANIFEST_NAME, zipper.MAILMERGE_NAME]
        raw = zf.read(zipper.MAILMERGE_NAME)
        manifest = _csv_rows(zf.read(zipper.MANIFEST_NAME))
        pdf_bytes = b"".join(zf.read(n) + decompressed(zf.read(n)) for n in pdfs)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = _csv_rows(raw)
    assert rows == [
        list(zipper.MAILMERGE_COLUMNS),
        [
            "maria.p@example.invalid",
            "Μαρία Παπαδοπούλου",
            "Ταξίδι 10GB",
            "8999000000000003017",
            "esim_8999000000000003017.pdf",
        ],
        [
            "Giorgos.N@mail.example.invalid",
            "Γιώργος Νικολάου",
            "Ταξίδι 10GB",
            "8999000000000003025",
            "esim_8999000000000003025.pdf",
        ],
        [
            "",
            "Νίκος Αθανασίου",
            "Ευρώπη 5GB",
            "8999000000000003041",
            "esim_8999000000000003041.pdf",
        ],
        [
            "maria.p@example.invalid",
            "Μαρία Παπαδοπούλου",
            "Ευρώπη 5GB",
            "8999000000000003058",
            "esim_8999000000000003058.pdf",
        ],
    ]
    # Same rows, same order and same file names as the manifest and the archive.
    assert [r[4] for r in rows[1:]] == [r[2] for r in manifest[1:]] == pdfs
    text = raw.decode("utf-8-sig")
    for marker in SECRET_MARKERS:
        assert marker not in text
    # Addresses appear only in mailmerge.csv: not in the PDFs, not in the manifest.
    assert b"maria.p@" not in pdf_bytes and b"Giorgos.N@" not in pdf_bytes
    assert all("@" not in cell for row in manifest for cell in row)


@pytest.mark.parametrize("name", ["valid_minimal.csv", "valid_full.csv", "valid_greek_names.csv"])
def test_pipeline_has_no_mailmerge_without_an_email_column(fixture_bytes, name: str) -> None:
    report, data = pipeline.process_upload(fixture_bytes(name), name, BRANDING)
    assert report.ok and data is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zipper.MAILMERGE_NAME not in zf.namelist()
        assert zf.namelist()[-1] == zipper.MANIFEST_NAME


def test_pipeline_invalid_email_blocks_the_zip(fixture_bytes) -> None:
    report, data = pipeline.process_upload(fixture_bytes(FIXTURE), FIXTURE, BRANDING)
    assert data is None and not report.ok
    assert [(i.row_number, i.code) for i in report.errors] == [(4, "email_format")]


def test_pipeline_mailmerge_is_deterministic() -> None:
    first = pipeline.process_upload(_valid_fixture(), "emails.csv", BRANDING)[1]
    assert first == pipeline.process_upload(_valid_fixture(), "emails.csv", BRANDING)[1]

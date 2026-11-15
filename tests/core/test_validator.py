from __future__ import annotations

import csv
import io

import pytest

from app.core import validator
from app.core.models import EsimRecord
from tests.conftest import FIXTURES_DIR, not_implemented

# Synthetic codes only. Host is a reserved .invalid domain.
HOST = "smdp.example.invalid"
VALID_AC = f"LPA:1${HOST}$TEST-0001-AAAA-BBBB"


def _rec(
    row: int,
    iccid: str = "8999000000000000013",
    activation_code: str = VALID_AC,
    **kwargs: str | None,
) -> EsimRecord:
    return EsimRecord(row_number=row, iccid=iccid, activation_code=activation_code, **kwargs)


def _load_fixture(name: str) -> list[EsimRecord]:
    """Build records straight from a fixture CSV (parser is not used here).

    Row numbers follow the spreadsheet convention: header is row 1.
    """
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8-sig")
    records = []
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        records.append(
            EsimRecord(
                row_number=i,
                iccid=row["iccid"],
                activation_code=row["activation_code"],
                confirmation_code=row.get("confirmation_code") or None,
                imsi=row.get("imsi") or None,
                msisdn=row.get("msisdn") or None,
                customer_name=row.get("customer_name") or None,
                plan_name=row.get("plan_name") or None,
            )
        )
    return records


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


# --- luhn_ok ------------------------------------------------------------------


@pytest.mark.parametrize(
    "digits",
    [
        "79927398713",
        "0",
        "18",
        "8999000000000000013",
        "8999000000000000104",
        "89990000000000008157",
    ],
)
def test_luhn_ok_known_good(digits: str) -> None:
    assert validator.luhn_ok(digits) is True


@pytest.mark.parametrize(
    "digits",
    ["79927398710", "79927398714", "8999000000000000709", "8999000000000000014", "", "12a4"],
)
def test_luhn_ok_known_bad(digits: str) -> None:
    assert validator.luhn_ok(digits) is False


# --- validate_iccid -----------------------------------------------------------


@pytest.mark.parametrize("iccid", ["8999000000000000013", "89990000000000008157"])
def test_validate_iccid_valid_19_and_20_digits(iccid: str) -> None:
    assert validator.validate_iccid(iccid) == []


def test_validate_iccid_wrong_check_digit() -> None:
    (iccid,) = [r.iccid for r in _load_fixture("invalid_iccid_luhn.csv")]
    assert validator.validate_iccid(iccid) == [("iccid_luhn", "error")]


def test_validate_iccid_wrong_lengths() -> None:
    rows = _load_fixture("invalid_iccid_length.csv")
    assert len(rows[0].iccid) == 18
    assert len(rows[1].iccid) == 21
    assert validator.validate_iccid(rows[0].iccid) == [("iccid_length", "error")]
    assert validator.validate_iccid(rows[1].iccid) == [("iccid_length", "error")]


@pytest.mark.parametrize(
    "iccid", ["8999ABC000000000081", "8999 00000000000013", "-899900000000000013"]
)
def test_validate_iccid_non_digit(iccid: str) -> None:
    assert validator.validate_iccid(iccid) == [("iccid_format", "error")]


def test_validate_iccid_empty() -> None:
    assert validator.validate_iccid("") == [("iccid_missing", "error")]


def test_validate_iccid_missing_89_prefix_is_warning_only() -> None:
    (iccid,) = [r.iccid for r in _load_fixture("warning_iccid_prefix.csv")]
    assert validator.validate_iccid(iccid) == [("iccid_prefix", "warning")]


# --- validate_activation_code -------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        VALID_AC,
        f"LPA:1${HOST}$X",
        # Optional SM-DP+ OID and confirmation-code-required flag.
        f"LPA:1${HOST}$TEST-0001$1.3.6.1.4.1.31746$1",
        # Empty OID with the flag present is allowed by SGP.22.
        f"LPA:1${HOST}$TEST-0001$$1",
    ],
)
def test_validate_activation_code_valid(code: str) -> None:
    assert validator.validate_activation_code(code) is None


@pytest.mark.parametrize(
    "code",
    [
        f"1${HOST}$TEST-0001",
        f"LPA:2${HOST}$TEST-0001",
        f"lpa:1${HOST}$TEST-0001",
        f"LPA1${HOST}$TEST-0001",
        "NOT-AN-LPA",
    ],
)
def test_validate_activation_code_missing_prefix(code: str) -> None:
    assert validator.validate_activation_code(code) == "activation_code_prefix"


@pytest.mark.parametrize("code", ["LPA:1$", f"LPA:1${HOST}"])
def test_validate_activation_code_missing_sections(code: str) -> None:
    assert validator.validate_activation_code(code) == "activation_code_structure"


def test_validate_activation_code_empty_host() -> None:
    assert validator.validate_activation_code("LPA:1$$TEST-0001") == "activation_code_host"


@pytest.mark.parametrize("code", [f"LPA:1${HOST}$", f"LPA:1${HOST}$$1.3.6.1"])
def test_validate_activation_code_empty_matching_id(code: str) -> None:
    assert validator.validate_activation_code(code) == "activation_code_matching_id"


def test_validate_activation_code_empty() -> None:
    assert validator.validate_activation_code("") == "activation_code_missing"


@pytest.mark.parametrize(
    "code",
    [
        f" {VALID_AC}",
        f"{VALID_AC} ",
        f"{VALID_AC}\n",
        "LPA:1$smdp.example .invalid$TEST-0001",
        f"LPA:1${HOST}$TEST 0001",
        f"LPA:1${HOST}$TEST\t0001",
        "   ",
    ],
)
def test_validate_activation_code_whitespace(code: str) -> None:
    assert validator.validate_activation_code(code) == "activation_code_whitespace"


def test_validate_activation_code_fixture() -> None:
    codes = [
        validator.validate_activation_code(r.activation_code)
        for r in _load_fixture("invalid_activation_code.csv")
    ]
    assert codes == [
        "activation_code_prefix",
        "activation_code_structure",
        "activation_code_matching_id",
        "activation_code_missing",
    ]


# --- validate_imsi / validate_msisdn -------------------------------------------


@pytest.mark.parametrize("imsi", ["001010000000030", "000000000000031", "202011234567890"])
def test_validate_imsi_valid_and_leading_zeros(imsi: str) -> None:
    assert validator.validate_imsi(imsi) is None


@pytest.mark.parametrize(
    "imsi",
    ["00101123", "0010112345678901", "00101ABCDEFGHIJ", "+01010000000030", "00101000000 030"],
)
def test_validate_imsi_invalid(imsi: str) -> None:
    assert validator.validate_imsi(imsi) == "imsi_format"


@pytest.mark.parametrize(
    "msisdn", ["+306900000030", "0030690000030", "0000000031", "1234567", "+123456789012345"]
)
def test_validate_msisdn_valid_and_leading_zeros(msisdn: str) -> None:
    assert validator.validate_msisdn(msisdn) is None


@pytest.mark.parametrize(
    "msisdn",
    ["+30", "123456", "+30690000000000000", "1234567890123456", "+3069ABC1234", "++30690000", "+"],
)
def test_validate_msisdn_invalid(msisdn: str) -> None:
    assert validator.validate_msisdn(msisdn) == "msisdn_format"


def test_imsi_msisdn_fixture_all_invalid() -> None:
    for rec in _load_fixture("invalid_imsi_msisdn.csv"):
        assert validator.validate_imsi(rec.imsi or "") == "imsi_format"
        assert validator.validate_msisdn(rec.msisdn or "") == "msisdn_format"


def test_leading_zeros_preserved_through_validate() -> None:
    records = _load_fixture("valid_leading_zeros.csv")
    report = validator.validate(records)
    assert report.issues == []
    assert [r.imsi for r in report.valid] == ["001010000000030", "000000000000031"]
    assert [r.msisdn for r in report.valid] == ["0030690000030", "0000000031"]
    assert all(isinstance(r.iccid, str) for r in report.valid)


# --- find_duplicates ----------------------------------------------------------


def test_find_duplicates_fixture() -> None:
    records = _load_fixture("duplicates.csv")
    issues = validator.find_duplicates(records)
    found = sorted((i.row_number, i.field, i.code, i.severity) for i in issues)
    assert found == [
        (3, "iccid", "duplicate_iccid", "error"),
        (4, "activation_code", "duplicate_activation_code", "error"),
    ]
    by_code = {i.code: i for i in issues}
    assert "row 2" in by_code["duplicate_iccid"].message
    assert "row 2" in by_code["duplicate_activation_code"].message
    for issue in issues:
        for rec in records:
            assert rec.activation_code not in issue.message
            assert rec.iccid not in issue.message


def test_find_duplicates_cites_first_row_for_each_repeat() -> None:
    records = [_rec(5), _rec(6, activation_code=f"LPA:1${HOST}$OTHER"), _rec(9)]
    issues = validator.find_duplicates(records)
    iccid_dups = [i for i in issues if i.code == "duplicate_iccid"]
    ac_dups = [i for i in issues if i.code == "duplicate_activation_code"]
    assert [i.row_number for i in iccid_dups] == [6, 9]
    assert all("row 5" in i.message for i in iccid_dups)
    assert [i.row_number for i in ac_dups] == [9]
    assert "row 5" in ac_dups[0].message


def test_find_duplicates_none() -> None:
    assert validator.find_duplicates(_load_fixture("valid_full.csv")) == []
    assert validator.find_duplicates([]) == []


def test_find_duplicates_ignores_empty_values() -> None:
    records = [_rec(2, iccid="", activation_code=""), _rec(3, iccid="", activation_code="")]
    assert validator.find_duplicates(records) == []


# --- validate() ---------------------------------------------------------------


def test_validate_empty() -> None:
    report = validator.validate([])
    assert report.valid == []
    assert report.issues == []


def test_validate_all_valid_fixture() -> None:
    records = _load_fixture("valid_full.csv")
    report = validator.validate(records)
    assert report.issues == []
    assert report.valid == records


def test_validate_mixed_errors_exclude_only_bad_rows() -> None:
    records = _load_fixture("mixed_valid_invalid.csv")
    report = validator.validate(records)
    errors = [i for i in report.issues if i.severity == "error"]
    assert sorted((i.row_number, i.field, i.code) for i in errors) == [
        (3, "iccid", "iccid_luhn"),
        (5, "activation_code", "activation_code_prefix"),
    ]
    assert [r.row_number for r in report.valid] == [2, 4]


def test_validate_warning_does_not_exclude_record() -> None:
    records = _load_fixture("warning_iccid_prefix.csv")
    report = validator.validate(records)
    assert [(i.code, i.severity, i.field) for i in report.issues] == [
        ("iccid_prefix", "warning", "iccid")
    ]
    assert report.valid == records


def test_validate_duplicates_are_errors() -> None:
    report = validator.validate(_load_fixture("duplicates.csv"))
    assert {i.code for i in report.issues} == {"duplicate_iccid", "duplicate_activation_code"}
    assert [r.row_number for r in report.valid] == [2]


def test_validate_reports_optional_fields_only_when_present() -> None:
    records = [
        _rec(2, imsi="123", msisdn="abc"),
        _rec(
            3,
            iccid="8999000000000000021",
            activation_code=f"LPA:1${HOST}$TEST-0002-AAAA-BBBB",
            imsi=None,
            msisdn="",
        ),
    ]
    report = validator.validate(records)
    assert sorted((i.row_number, i.field, i.code) for i in report.issues) == [
        (2, "imsi", "imsi_format"),
        (2, "msisdn", "msisdn_format"),
    ]
    assert [r.row_number for r in report.valid] == [3]


def test_validate_issue_messages_are_nonempty_and_rows_correct() -> None:
    report = validator.validate(_load_fixture("invalid_imsi_msisdn.csv"))
    assert {i.row_number for i in report.issues} == {2, 3, 4}
    assert all(i.message for i in report.issues)


@not_implemented
def test_validate_report_ok_with_only_warnings() -> None:
    # ValidationReport.ok/errors/warnings live in models.py, still a stub.
    report = validator.validate(_load_fixture("warning_iccid_prefix.csv"))
    assert report.ok is True
    assert report.errors == []
    assert len(report.warnings) == 1


@not_implemented
def test_validate_report_not_ok_with_errors() -> None:
    report = validator.validate(_load_fixture("mixed_valid_invalid.csv"))
    assert report.ok is False
    assert len(report.errors) == 2


# --- secrecy ------------------------------------------------------------------

ALL_FIXTURES = [
    "valid_full.csv",
    "valid_minimal.csv",
    "valid_leading_zeros.csv",
    "duplicates.csv",
    "invalid_activation_code.csv",
    "invalid_iccid_length.csv",
    "invalid_iccid_luhn.csv",
    "invalid_imsi_msisdn.csv",
    "mixed_valid_invalid.csv",
    "warning_iccid_prefix.csv",
]


def _secret_fragments(rec: EsimRecord) -> list[str]:
    parts = [rec.activation_code, rec.confirmation_code or ""]
    # Also the matching id alone (the secret part of the LPA string).
    parts.extend(p for p in rec.activation_code.split("$")[2:3])
    return [p for p in parts if len(p) >= 4]


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_message_contains_secrets_fixtures(name: str) -> None:
    records = _load_fixture(name)
    report = validator.validate(records)
    for issue in report.issues:
        for rec in records:
            for secret in _secret_fragments(rec):
                assert secret not in issue.message


def test_no_message_contains_secrets_handcrafted() -> None:
    matching_id = "SECRET-MATCH-9F3A"
    confirmation = "CONF-SECRET-77"
    bad_codes = [
        f"LPA:1${HOST}${matching_id} ",
        f"XLPA:1${HOST}${matching_id}",
        f"LPA:1${matching_id}",
        f"LPA:1$${matching_id}",
    ]
    records = [
        _rec(i, iccid="9999000000000000608", activation_code=c, confirmation_code=confirmation)
        for i, c in enumerate(bad_codes, start=2)
    ]
    # Add a duplicate so duplicate messages are covered too.
    records.append(_rec(10, activation_code=bad_codes[0], confirmation_code=confirmation))
    report = validator.validate(records)
    assert len(report.issues) >= len(records)
    for issue in report.issues:
        assert matching_id not in issue.message
        assert confirmation not in issue.message
        for code in bad_codes:
            assert code.strip() not in issue.message


def test_validate_activation_code_return_never_echoes_value() -> None:
    value = f"LPA:1${HOST}$SECRET 9F3A"
    result = validator.validate_activation_code(value)
    assert result is not None
    assert "SECRET" not in result

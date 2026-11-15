from __future__ import annotations

import pytest

from app.core import parser
from app.core.models import EsimRecord, ParseError
from tests.conftest import synthetic_csv

AC1 = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"
AC2 = "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB"
ICCID1 = "8999000000000000013"
ICCID2 = "8999000000000000021"

SECRETS = ("TEST-", "CONF", "LPA:1$", "smdp.example.invalid")


def _csv(text: str) -> list[EsimRecord]:
    return parser.parse_csv(text.encode("utf-8"))


def _assert_no_secrets(exc: pytest.ExceptionInfo[ParseError]) -> None:
    message = str(exc.value)
    for secret in SECRETS:
        assert secret not in message


# --- parse_upload -------------------------------------------------------------


def test_parse_upload_csv(fixture_bytes) -> None:
    records = parser.parse_upload(fixture_bytes("valid_minimal.csv"), "valid_minimal.csv")
    assert [r.iccid for r in records] == [
        "8999000000000000013",
        "8999000000000000021",
        "8999000000000000039",
    ]
    assert [r.row_number for r in records] == [2, 3, 4]


@pytest.mark.parametrize("filename", ["UPLOAD.CSV", "upload.Csv", "dir/my.file.csv"])
def test_parse_upload_csv_extension_case_insensitive(fixture_bytes, filename) -> None:
    assert len(parser.parse_upload(fixture_bytes("valid_minimal.csv"), filename)) == 3


@pytest.mark.parametrize("filename", ["upload.xlsx", "UPLOAD.XLSX"])
def test_parse_upload_xlsx(valid_xlsx, filename) -> None:
    records = parser.parse_upload(valid_xlsx, filename)
    assert [r.iccid for r in records] == [ICCID1, ICCID2]


@pytest.mark.parametrize(
    "filename", ["unsupported.txt", "codes.xls", "codes", "codes.csv.txt", "codes.json"]
)
def test_parse_upload_unsupported_extension(fixture_bytes, filename) -> None:
    with pytest.raises(ParseError):
        parser.parse_upload(fixture_bytes("unsupported.txt"), filename)


def test_parse_upload_csv_bytes_named_xlsx_is_parse_error(fixture_bytes) -> None:
    with pytest.raises(ParseError):
        parser.parse_upload(fixture_bytes("valid_minimal.csv"), "valid_minimal.xlsx")


# --- parse_csv ----------------------------------------------------------------


def test_parse_csv_full(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("valid_full.csv"))
    assert len(records) == 5
    first = records[0]
    assert first == EsimRecord(
        row_number=2,
        iccid="8999000000000000104",
        activation_code="LPA:1$smdp.example.invalid$TEST-0010-AAAA-BBBB",
        confirmation_code="CONF0010",
        imsi="001010000000010",
        msisdn="+306900000010",
        customer_name="Test Customer 10",
        plan_name="Test Plan 5GB",
    )
    for r in records:
        assert isinstance(r.iccid, str)
        assert isinstance(r.imsi, str)
        assert isinstance(r.msisdn, str)
        assert r.confirmation_code is not None
        assert r.confirmation_code not in r.activation_code


def test_parse_csv_bom_and_semicolon(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("valid_bom_semicolon.csv"))
    assert [r.iccid for r in records] == [
        "8999000000000000401",
        "8999000000000000419",
        "8999000000000000427",
    ]
    assert records[0].activation_code == "LPA:1$smdp.example.invalid$TEST-0040-AAAA-BBBB"
    assert records[0].customer_name == "Test Customer 40"


def test_parse_csv_greek_names(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("valid_greek_names.csv"))
    assert records[0].customer_name == "Γιώργος Παπαδόπουλος"
    assert records[2].plan_name == "Ταξίδι 1GB"


def test_parse_csv_leading_zeros_preserved(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("valid_leading_zeros.csv"))
    assert records[0].imsi == "001010000000030"
    assert records[0].msisdn == "0030690000030"
    assert records[1].imsi == "000000000000031"
    assert records[1].msisdn == "0000000031"


def test_parse_csv_split_smdp(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("valid_split_smdp.csv"))
    assert [r.activation_code for r in records] == [
        "LPA:1$smdp.example.invalid$TEST-0020-SPLIT",
        "LPA:1$smdp.example.invalid$TEST-0021-SPLIT",
        "LPA:1$smdp.example.invalid$TEST-0022-SPLIT",
    ]
    assert all(r.confirmation_code is None for r in records)


def test_parse_csv_split_never_includes_confirmation_code() -> None:
    (rec,) = _csv(
        "iccid,smdp_address,matching_id,confirmation_code\n"
        f"{ICCID1},smdp.example.invalid,TEST-0001,CONF-SECRET\n"
    )
    assert rec.activation_code == "LPA:1$smdp.example.invalid$TEST-0001"
    assert "CONF-SECRET" not in rec.activation_code
    assert rec.confirmation_code == "CONF-SECRET"


def test_parse_csv_activation_code_wins_over_split() -> None:
    records = _csv(
        "iccid,activation_code,smdp_address,matching_id\n"
        f"{ICCID1},{AC1},other.example.invalid,TEST-OTHER\n"
        f"{ICCID2},,smdp.example.invalid,TEST-0002-AAAA-BBBB\n"
    )
    assert records[0].activation_code == AC1
    # Row without a full code falls back to the split pair.
    assert records[1].activation_code == AC2


def test_parse_csv_missing_activation_is_left_for_validator() -> None:
    (rec,) = _csv(f"iccid,activation_code\n{ICCID1},\n")
    assert rec.activation_code == ""


def test_parse_csv_blank_and_whitespace_rows(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("blank_and_whitespace_rows.csv"))
    assert records == [
        EsimRecord(
            row_number=2,
            iccid="8999000000000001300",
            activation_code="LPA:1$smdp.example.invalid$TEST-0130-AAAA-BBBB",
            customer_name="Test Customer 130",
        ),
        EsimRecord(
            row_number=5,
            iccid="8999000000000001318",
            activation_code="LPA:1$smdp.example.invalid$TEST-0131-AAAA-BBBB",
            customer_name="Test Customer 131",
        ),
    ]


def test_parse_csv_row_numbers_are_source_rows_with_crlf() -> None:
    data = f"iccid,activation_code\r\n\r\n{ICCID1},{AC1}\r\n,\r\n{ICCID2},{AC2}\r\n".encode()
    records = parser.parse_csv(data)
    assert [r.row_number for r in records] == [3, 5]


def test_parse_csv_empty_strings_become_none() -> None:
    (rec,) = _csv(
        "iccid,activation_code,confirmation_code,imsi,msisdn,customer_name,plan_name\n"
        f"{ICCID1},{AC1},  ,,   ,,\n"
    )
    assert rec.confirmation_code is None
    assert rec.imsi is None
    assert rec.msisdn is None
    assert rec.customer_name is None
    assert rec.plan_name is None


def test_parse_csv_short_rows_missing_cells_are_none() -> None:
    (rec,) = _csv(f"iccid,activation_code,customer_name\n{ICCID1},{AC1}\n")
    assert rec.customer_name is None


def test_parse_csv_empty_fixture_yields_no_records(fixture_bytes) -> None:
    assert parser.parse_csv(fixture_bytes("empty.csv")) == []


@pytest.mark.parametrize("data", [b"", b"\xef\xbb\xbf", b"\n\n"])
def test_parse_csv_no_header_is_parse_error(data) -> None:
    with pytest.raises(ParseError):
        parser.parse_csv(data)


def test_parse_csv_not_utf8(fixture_bytes) -> None:
    with pytest.raises(ParseError, match="UTF-8"):
        parser.parse_csv(fixture_bytes("not_utf8.csv"))


def test_parse_csv_missing_activation_columns(fixture_bytes) -> None:
    with pytest.raises(ParseError, match="activation_code"):
        parser.parse_csv(fixture_bytes("missing_activation_columns.csv"))


def test_parse_csv_injection_names_kept_verbatim(fixture_bytes) -> None:
    records = parser.parse_csv(fixture_bytes("csv_injection_names.csv"))
    assert records[0].customer_name == '=HYPERLINK("http://example.invalid")'
    assert records[3].customer_name == "@SUM(A1)"


def test_parse_csv_quoted_comma_with_comma_delimiter() -> None:
    (rec,) = _csv(f'iccid,activation_code,customer_name\n{ICCID1},{AC1},"Doe, Jane"\n')
    assert rec.customer_name == "Doe, Jane"


def test_parse_csv_semicolon_with_commas_in_values() -> None:
    (rec,) = _csv(f"iccid;activation_code;customer_name\n{ICCID1};{AC1};Doe, Jane\n")
    assert rec.customer_name == "Doe, Jane"


# --- row limit ------------------------------------------------------------------


def test_parse_csv_too_many_rows(too_many_rows_csv) -> None:
    with pytest.raises(ParseError, match=str(parser.MAX_ROWS)):
        parser.parse_csv(too_many_rows_csv)


def test_parse_csv_exactly_max_rows_is_ok() -> None:
    assert len(parser.parse_csv(synthetic_csv(parser.MAX_ROWS))) == parser.MAX_ROWS


def test_parse_csv_blank_rows_do_not_count_towards_limit() -> None:
    body = synthetic_csv(parser.MAX_ROWS).decode() + "\n,\n\n" * 10
    assert len(parser.parse_csv(body.encode())) == parser.MAX_ROWS


def test_parse_xlsx_too_many_rows(xlsx_bytes) -> None:
    rows: list[list[object]] = [["iccid", "activation_code"]]
    rows += [[f"{i:019d}", AC1] for i in range(parser.MAX_ROWS + 1)]
    with pytest.raises(ParseError, match=str(parser.MAX_ROWS)):
        parser.parse_xlsx(xlsx_bytes(rows))


@pytest.fixture
def iter_rows_spy(monkeypatch) -> list[dict[str, object]]:
    """Record each read-only ``iter_rows`` call and how many rows it yielded."""
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    calls: list[dict[str, object]] = []
    original = ReadOnlyWorksheet.iter_rows

    def spy(self, *args, **kwargs):
        call: dict[str, object] = {"kwargs": kwargs, "yielded": 0}
        calls.append(call)
        for row in original(self, *args, **kwargs):
            call["yielded"] += 1
            yield row

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", spy)
    return calls


def test_parse_xlsx_stops_reading_at_limit(xlsx_bytes, iter_rows_spy) -> None:
    rows: list[list[object]] = [["iccid", "activation_code"]]
    rows += [[f"{i:019d}", AC1] for i in range(parser.MAX_ROWS + 50)]
    # A numeric ICCID past the limit: only reachable if the whole sheet were read.
    rows[-1][0] = 8999000000000000013
    with pytest.raises(ParseError, match="Too many rows"):
        parser.parse_xlsx(xlsx_bytes(rows))
    data_call = iter_rows_spy[-1]
    assert data_call["kwargs"]["min_row"] == 2
    assert data_call["yielded"] == parser.MAX_ROWS + 1


def test_parse_xlsx_reads_only_known_columns(xlsx_bytes, iter_rows_spy) -> None:
    data = xlsx_bytes(
        [
            ["notes", "iccid", "activation_code", "extra", "more"],
            ["n", ICCID1, AC1, 1, 2],
            ["only unknown data", None, None, 3, 4],
        ]
    )
    records = parser.parse_xlsx(data)
    assert [r.row_number for r in records] == [2]
    assert iter_rows_spy[-1]["kwargs"]["max_col"] == 3


def test_openpyxl_uses_defusedxml() -> None:
    import openpyxl.xml

    assert openpyxl.xml.DEFUSEDXML is True


# --- normalization ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lpa:1$smdp.example.invalid$TEST-0001", "LPA:1$smdp.example.invalid$TEST-0001"),
        ("Lpa:1$smdp.example.invalid$TEST-0001", "LPA:1$smdp.example.invalid$TEST-0001"),
        ("  LPA:1$smdp.example.invalid$test-0001  ", "LPA:1$smdp.example.invalid$test-0001"),
        # Nothing but the prefix changes; a malformed prefix is left for the validator.
        ("LPA:2$smdp.example.invalid$TEST-0001", "LPA:2$smdp.example.invalid$TEST-0001"),
        ("1$smdp.example.invalid$TEST-0001", "1$smdp.example.invalid$TEST-0001"),
        ("LPA:1$Smdp.Example.Invalid$Te St", "LPA:1$Smdp.Example.Invalid$Te St"),
    ],
)
def test_activation_code_prefix_normalization(raw, expected) -> None:
    (rec,) = _csv(f"iccid,activation_code\n{ICCID1},{raw}\n")
    assert rec.activation_code == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+30 690 000 0001", "+306900000001"),
        ("+30-690-000-0001", "+306900000001"),
        ("(030) 690-000 0001", "0306900000001"),
        ("  +306900000001  ", "+306900000001"),
        ("0030690000001", "0030690000001"),
        ("+30.690", "+30.690"),
    ],
)
def test_msisdn_normalization(raw, expected) -> None:
    (rec,) = _csv(f'iccid,activation_code,msisdn\n{ICCID1},{AC1},"{raw}"\n')
    assert rec.msisdn == expected


def test_msisdn_only_separators_becomes_none() -> None:
    (rec,) = _csv(f'iccid,activation_code,msisdn\n{ICCID1},{AC1}," - ( ) "\n')
    assert rec.msisdn is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  8999000000000000013  ", "8999000000000000013"),
        ("8999 0000 0000 0000 013", "8999 0000 0000 0000 013"),
        ("8999-0000-0000-0000-013", "8999-0000-0000-0000-013"),
        ("0899900000000000001", "0899900000000000001"),
    ],
)
def test_iccid_and_imsi_trim_only(raw, expected) -> None:
    (rec,) = _csv(f'iccid,activation_code,imsi\n"{raw}",{AC1},"{raw}"\n')
    assert rec.iccid == expected
    assert rec.imsi == expected


def test_split_fields_are_trimmed() -> None:
    (rec,) = _csv(f"iccid,smdp_address,matching_id\n{ICCID1},  smdp.example.invalid , TEST-01 \n")
    assert rec.activation_code == "LPA:1$smdp.example.invalid$TEST-01"


# --- parse_xlsx ---------------------------------------------------------------


def test_parse_xlsx_valid(valid_xlsx) -> None:
    records = parser.parse_xlsx(valid_xlsx)
    assert records == [
        EsimRecord(
            row_number=2,
            iccid=ICCID1,
            activation_code=AC1,
            confirmation_code="CONF0001",
            imsi="001010000000001",
            msisdn="+306900000001",
            customer_name="Test Customer 1",
        ),
        EsimRecord(
            row_number=3,
            iccid=ICCID2,
            activation_code=AC2,
            imsi="001010000000002",
            msisdn="0030690000002",
            customer_name="Test Customer 2",
        ),
    ]


def test_parse_xlsx_numeric_iccid_rejected(numeric_iccid_xlsx) -> None:
    with pytest.raises(ParseError) as exc:
        parser.parse_xlsx(numeric_iccid_xlsx)
    message = str(exc.value)
    assert "3" in message
    assert "iccid" in message
    assert "8999000000000000021" not in message


@pytest.mark.parametrize(
    "column",
    ["iccid", "imsi", "msisdn", "activation_code", "matching_id", "confirmation_code"],
)
@pytest.mark.parametrize("number", [12345, 1.5])
def test_parse_xlsx_numeric_identifier_rejected(xlsx_bytes, column, number) -> None:
    header = ["iccid", "smdp_address", "matching_id", "confirmation_code", "imsi", "msisdn"]
    row: list[object] = [
        ICCID1,
        "smdp.example.invalid",
        "TEST-0001",
        "CONF0001",
        "001010000000001",
        "+306900000001",
    ]
    if column == "activation_code":
        header.append("activation_code")
        row.append(AC1)
    row[header.index(column)] = number
    data = xlsx_bytes([header, ["8999000000000000039", "h", "m"], row])
    with pytest.raises(ParseError) as exc:
        parser.parse_xlsx(data)
    message = str(exc.value)
    assert "row 3" in message.lower()
    assert column in message
    assert str(number) not in message


def test_parse_xlsx_numeric_non_identifier_is_text(xlsx_bytes) -> None:
    (rec,) = parser.parse_xlsx(
        xlsx_bytes([["iccid", "activation_code", "plan_name"], [ICCID1, AC1, 5]])
    )
    assert rec.plan_name == "5"


def test_parse_xlsx_first_sheet_only_and_unknown_columns(extra_sheets_and_columns_xlsx) -> None:
    records = parser.parse_xlsx(extra_sheets_and_columns_xlsx)
    assert records == [
        EsimRecord(row_number=2, iccid=ICCID1, activation_code=AC1, plan_name="Test Plan")
    ]


def test_parse_xlsx_blank_rows_and_normalization(xlsx_bytes) -> None:
    data = xlsx_bytes(
        [
            [" ICCID ", "Activation_Code", "MSISDN", "Confirmation_Code"],
            [None, None, None, None],
            [f"  {ICCID1} ", "lpa:1$smdp.example.invalid$TEST-0001-AAAA-BBBB", "+30 (690) 1", " "],
            ["   ", "", None, None],
            [ICCID2, AC2, None, None],
        ]
    )
    records = parser.parse_xlsx(data)
    assert [r.row_number for r in records] == [3, 5]
    assert records[0].iccid == ICCID1
    assert records[0].activation_code == AC1
    assert records[0].msisdn == "+306901"
    assert records[0].confirmation_code is None


def test_parse_xlsx_split_activation(xlsx_bytes) -> None:
    (rec,) = parser.parse_xlsx(
        xlsx_bytes(
            [
                ["iccid", "smdp_address", "matching_id", "confirmation_code"],
                [ICCID1, "smdp.example.invalid", "TEST-0001-AAAA-BBBB", "CONF"],
            ]
        )
    )
    assert rec.activation_code == AC1
    assert rec.confirmation_code == "CONF"


def test_parse_xlsx_header_only(xlsx_bytes) -> None:
    assert parser.parse_xlsx(xlsx_bytes([["iccid", "activation_code"]])) == []


def test_parse_xlsx_empty_sheet(xlsx_bytes) -> None:
    with pytest.raises(ParseError):
        parser.parse_xlsx(xlsx_bytes([]))


def test_parse_xlsx_missing_columns(xlsx_bytes) -> None:
    with pytest.raises(ParseError, match="iccid"):
        parser.parse_xlsx(xlsx_bytes([["activation_code"], [AC1]]))


def test_parse_xlsx_corrupt_fixture(corrupt_xlsx) -> None:
    with pytest.raises(ParseError):
        parser.parse_xlsx(corrupt_xlsx)


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04garbage"])
def test_parse_xlsx_corrupt(data) -> None:
    with pytest.raises(ParseError):
        parser.parse_xlsx(data)


# --- normalize_headers ----------------------------------------------------------


def test_normalize_headers_case_and_whitespace() -> None:
    assert parser.normalize_headers([" ICCID ", "Activation_Code", "Customer_NAME"]) == {
        "iccid": 0,
        "activation_code": 1,
        "customer_name": 2,
    }


def test_normalize_headers_ignores_unknown_columns() -> None:
    assert parser.normalize_headers(["notes", "iccid", "", "x", "smdp_address", "matching_id"]) == {
        "iccid": 1,
        "smdp_address": 4,
        "matching_id": 5,
    }


def test_normalize_headers_unknown_duplicates_are_ignored() -> None:
    assert parser.normalize_headers(["notes", "iccid", "notes", "activation_code"]) == {
        "iccid": 1,
        "activation_code": 3,
    }


def test_normalize_headers_all_known_columns() -> None:
    headers = [
        "iccid",
        "activation_code",
        "smdp_address",
        "matching_id",
        "confirmation_code",
        "imsi",
        "msisdn",
        "customer_name",
        "plan_name",
    ]
    assert parser.normalize_headers(headers) == {h: i for i, h in enumerate(headers)}


@pytest.mark.parametrize(
    "headers",
    [
        ["iccid", "activation_code", "ICCID"],
        ["iccid", "activation_code", " activation_code "],
        ["iccid", "smdp_address", "matching_id", "Matching_ID"],
    ],
)
def test_normalize_headers_duplicates(headers) -> None:
    with pytest.raises(ParseError, match="more than once"):
        parser.normalize_headers(headers)


@pytest.mark.parametrize(
    "headers",
    [
        ["activation_code"],
        [],
        ["iccid"],
        ["iccid", "smdp_address"],
        ["iccid", "matching_id", "confirmation_code"],
    ],
)
def test_normalize_headers_missing_required(headers) -> None:
    with pytest.raises(ParseError):
        parser.normalize_headers(headers)


def test_normalize_headers_activation_code_alone_is_enough() -> None:
    assert parser.normalize_headers(["iccid", "activation_code", "smdp_address"]) == {
        "iccid": 0,
        "activation_code": 1,
        "smdp_address": 2,
    }


# --- build_activation_code ----------------------------------------------------


def test_build_activation_code() -> None:
    assert (
        parser.build_activation_code("smdp.example.invalid", "TEST-0001")
        == "LPA:1$smdp.example.invalid$TEST-0001"
    )


def test_build_activation_code_has_no_confirmation_slot() -> None:
    code = parser.build_activation_code("smdp.example.invalid", "TEST-0001")
    assert code.count("$") == 2


# --- secrets never appear in errors ---------------------------------------------


def test_errors_do_not_echo_secret_values(xlsx_bytes) -> None:
    data = xlsx_bytes(
        [
            ["iccid", "activation_code", "confirmation_code"],
            [ICCID1, "LPA:1$smdp.example.invalid$TEST-SECRET", 4242],
        ]
    )
    with pytest.raises(ParseError) as exc:
        parser.parse_xlsx(data)
    _assert_no_secrets(exc)
    assert "4242" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_too_many_rows_error_has_no_secrets(too_many_rows_csv) -> None:
    with pytest.raises(ParseError) as exc:
        parser.parse_csv(too_many_rows_csv)
    _assert_no_secrets(exc)

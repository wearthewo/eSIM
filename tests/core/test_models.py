from __future__ import annotations

import pytest

from app.core.models import EsimRecord, ValidationIssue, ValidationReport

# Synthetic values only. Host is a reserved .invalid domain.
MATCHING_ID = "SECRET-MATCH-4C2E"
ACTIVATION = f"LPA:1$smdp.example.invalid${MATCHING_ID}"
CONFIRMATION = "CONF-SECRET-31"


def _rec(**kwargs: object) -> EsimRecord:
    base: dict[str, object] = {
        "row_number": 2,
        "iccid": "8999000000000000013",
        "activation_code": ACTIVATION,
    }
    base.update(kwargs)
    return EsimRecord(**base)  # type: ignore[arg-type]


def _issue(severity: str, row: int = 2) -> ValidationIssue:
    return ValidationIssue(
        row_number=row,
        field="iccid",
        code="x",
        message="m",
        severity=severity,  # type: ignore[arg-type]
    )


# --- EsimRecord.__repr__ --------------------------------------------------------


@pytest.mark.parametrize("confirmation", [CONFIRMATION, None])
def test_repr_never_contains_activation_or_confirmation_code(confirmation: str | None) -> None:
    rec = _rec(confirmation_code=confirmation, imsi="001010000000030", msisdn="+306900000030")
    for text in (repr(rec), str(rec), f"{rec}", f"{rec!r}", repr([rec]), repr({"r": rec})):
        assert ACTIVATION not in text
        assert MATCHING_ID not in text
        assert "LPA:1$" not in text
        assert CONFIRMATION not in text


def test_repr_shows_non_secret_fields() -> None:
    rec = _rec(
        confirmation_code=CONFIRMATION,
        imsi="001010000000030",
        msisdn="0030690000030",
        customer_name="Test Customer",
        plan_name="Plan A",
    )
    text = repr(rec)
    assert text.startswith("EsimRecord(")
    assert "row_number=2" in text
    assert "iccid='8999000000000000013'" in text
    assert "imsi='001010000000030'" in text
    assert "msisdn='0030690000030'" in text
    assert "customer_name='Test Customer'" in text
    assert "plan_name='Plan A'" in text
    assert "activation_code='<redacted>'" in text
    assert "confirmation_code='<redacted>'" in text


def test_repr_shows_absent_confirmation_as_none() -> None:
    text = repr(_rec())
    assert "confirmation_code=None" in text
    assert "activation_code='<redacted>'" in text


def test_repr_does_not_alter_stored_values() -> None:
    rec = _rec(confirmation_code=CONFIRMATION)
    repr(rec)
    assert rec.activation_code == ACTIVATION
    assert rec.confirmation_code == CONFIRMATION


# --- ValidationReport -------------------------------------------------------------


def test_report_empty_is_ok() -> None:
    report = ValidationReport()
    assert report.ok is True
    assert report.errors == []
    assert report.warnings == []


def test_report_only_warnings_is_ok() -> None:
    w1, w2 = _issue("warning", 2), _issue("warning", 3)
    report = ValidationReport(issues=[w1, w2])
    assert report.ok is True
    assert report.errors == []
    assert report.warnings == [w1, w2]


def test_report_errors_and_warnings_split_in_order() -> None:
    e1, w1, e2 = _issue("error", 2), _issue("warning", 3), _issue("error", 4)
    report = ValidationReport(valid=[_rec(row_number=3)], issues=[e1, w1, e2])
    assert report.ok is False
    assert report.errors == [e1, e2]
    assert report.warnings == [w1]


def test_report_single_error_is_not_ok() -> None:
    assert ValidationReport(issues=[_issue("error")]).ok is False

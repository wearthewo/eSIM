"""Field-level and batch-level validation of parsed records.

Issue messages identify rows and fields only. They never echo activation or
confirmation codes.
"""

from __future__ import annotations

import re

from app.core.models import EsimRecord, Severity, ValidationIssue, ValidationReport

LPA_PREFIX = "LPA:1$"
TELECOM_PREFIX = "89"

_DIGITS = re.compile(r"[0-9]+")
_IMSI = re.compile(r"[0-9]{15}")
_MSISDN = re.compile(r"\+?[0-9]{7,15}")
_WHITESPACE = re.compile(r"\s")

# Fixed messages keyed by issue code. None of them interpolate field values.
MESSAGES: dict[str, str] = {
    "iccid_missing": "ICCID is missing.",
    "iccid_format": "ICCID must contain digits only.",
    "iccid_length": "ICCID must be 19 or 20 digits.",
    "iccid_luhn": "ICCID check digit is wrong (Luhn check failed).",
    "iccid_prefix": "ICCID does not start with the 89 telecom prefix.",
    "imsi_format": "IMSI must be exactly 15 digits.",
    "msisdn_format": "MSISDN must be an optional + followed by 7 to 15 digits.",
    "activation_code_missing": "Activation code is missing.",
    "activation_code_whitespace": "Activation code must not contain spaces or line breaks.",
    "activation_code_prefix": 'Activation code must start with "LPA:1$".',
    "activation_code_structure": (
        "Activation code must have the form LPA:1$<SM-DP+ address>$<matching ID>."
    ),
    "activation_code_host": "Activation code has an empty SM-DP+ address.",
    "activation_code_matching_id": "Activation code has an empty matching ID.",
}


def validate(records: list[EsimRecord]) -> ValidationReport:
    """Run every field check plus duplicate detection.

    A record is ``valid`` if it has no error-severity issues. Warnings are
    reported but do not exclude the record.
    """
    issues: list[ValidationIssue] = []
    for rec in records:
        issues.extend(_record_issues(rec))
    issues.extend(find_duplicates(records))

    error_rows = {i.row_number for i in issues if i.severity == "error"}
    valid = [r for r in records if r.row_number not in error_rows]
    return ValidationReport(valid=valid, issues=issues)


def _record_issues(rec: EsimRecord) -> list[ValidationIssue]:
    row = rec.row_number
    issues = [_issue(row, "iccid", code, sev) for code, sev in validate_iccid(rec.iccid)]

    code = validate_activation_code(rec.activation_code)
    if code:
        issues.append(_issue(row, "activation_code", code))
    if rec.imsi and (code := validate_imsi(rec.imsi)):
        issues.append(_issue(row, "imsi", code))
    if rec.msisdn and (code := validate_msisdn(rec.msisdn)):
        issues.append(_issue(row, "msisdn", code))
    return issues


def _issue(row: int, field: str, code: str, severity: Severity = "error") -> ValidationIssue:
    return ValidationIssue(
        row_number=row, field=field, code=code, message=MESSAGES[code], severity=severity
    )


def validate_iccid(value: str) -> list[tuple[str, Severity]]:
    """Check an ICCID string and return a list of ``(issue_code, severity)``.

    Errors: not 19-20 digits, failed Luhn check digit.
    Warning: does not start with the ``89`` telecom prefix.
    """
    if not value:
        return [("iccid_missing", "error")]
    if not _DIGITS.fullmatch(value):
        return [("iccid_format", "error")]
    if len(value) not in (19, 20):
        return [("iccid_length", "error")]

    found: list[tuple[str, Severity]] = []
    if not luhn_ok(value):
        found.append(("iccid_luhn", "error"))
    if not value.startswith(TELECOM_PREFIX):
        found.append(("iccid_prefix", "warning"))
    return found


def validate_imsi(value: str) -> str | None:
    """Return an issue code if ``value`` is not exactly 15 digits, else None."""
    return None if _IMSI.fullmatch(value) else "imsi_format"


def validate_msisdn(value: str) -> str | None:
    """Return an issue code unless ``value`` is an optional ``+`` then 7-15 digits."""
    return None if _MSISDN.fullmatch(value) else "msisdn_format"


def validate_activation_code(value: str) -> str | None:
    """Check GSMA SGP.22 LPA structure: ``LPA:1$<host>$<matching id>[$...]``.

    Checks structure only. Returns an issue code or None. Never echoes the value.
    Extra ``$`` fields (SM-DP+ OID, confirmation-code-required flag) are allowed
    and may be empty.
    """
    if not value:
        return "activation_code_missing"
    if _WHITESPACE.search(value):
        return "activation_code_whitespace"
    if not value.startswith(LPA_PREFIX):
        return "activation_code_prefix"

    sections = value[len(LPA_PREFIX) :].split("$")
    if len(sections) < 2:
        return "activation_code_structure"
    host, matching_id = sections[0], sections[1]
    if not host:
        return "activation_code_host"
    if not matching_id:
        return "activation_code_matching_id"
    return None


def luhn_ok(digits: str) -> bool:
    """Return True if the digit string passes the Luhn mod-10 check."""
    if not _DIGITS.fullmatch(digits):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_duplicates(records: list[EsimRecord]) -> list[ValidationIssue]:
    """Flag repeated ICCIDs and repeated activation codes (errors), citing row numbers only.

    Each repeat is flagged on its own row and names the row where the value was
    first seen. Empty values are ignored (they are reported as missing elsewhere).
    """
    issues: list[ValidationIssue] = []
    first_iccid: dict[str, int] = {}
    first_code: dict[str, int] = {}
    for rec in records:
        row = rec.row_number
        if rec.iccid:
            first = first_iccid.setdefault(rec.iccid, row)
            if first != row:
                issues.append(
                    ValidationIssue(
                        row_number=row,
                        field="iccid",
                        code="duplicate_iccid",
                        message=f"Duplicate ICCID; first seen in row {first}.",
                    )
                )
        if rec.activation_code:
            first = first_code.setdefault(rec.activation_code, row)
            if first != row:
                issues.append(
                    ValidationIssue(
                        row_number=row,
                        field="activation_code",
                        code="duplicate_activation_code",
                        message=f"Duplicate activation code; first seen in row {first}.",
                    )
                )
    return issues

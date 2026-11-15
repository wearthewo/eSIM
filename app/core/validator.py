"""Field-level and batch-level validation of parsed records.

Issue messages identify rows and fields only. They never echo activation or
confirmation codes.
"""

from __future__ import annotations

from app.core.models import EsimRecord, Severity, ValidationIssue, ValidationReport


def validate(records: list[EsimRecord]) -> ValidationReport:
    """Run every field check plus duplicate detection.

    A record is ``valid`` if it has no error-severity issues. Warnings are
    reported but do not exclude the record.
    """
    raise NotImplementedError


def validate_iccid(value: str) -> list[tuple[str, Severity]]:
    """Check an ICCID string and return a list of ``(issue_code, severity)``.

    Errors: not 19-20 digits, failed Luhn check digit.
    Warning: does not start with the ``89`` telecom prefix.
    """
    raise NotImplementedError


def validate_imsi(value: str) -> str | None:
    """Return an issue code if ``value`` is not exactly 15 digits, else None."""
    raise NotImplementedError


def validate_msisdn(value: str) -> str | None:
    """Return an issue code unless ``value`` is an optional ``+`` then 7-15 digits."""
    raise NotImplementedError


def validate_activation_code(value: str) -> str | None:
    """Check GSMA SGP.22 LPA structure: ``LPA:1$<host>$<matching id>[$...]``.

    Checks structure only. Returns an issue code or None. Never echoes the value.
    """
    raise NotImplementedError


def luhn_ok(digits: str) -> bool:
    """Return True if the digit string passes the Luhn mod-10 check."""
    raise NotImplementedError


def find_duplicates(records: list[EsimRecord]) -> list[ValidationIssue]:
    """Flag repeated ICCIDs and repeated activation codes (errors), citing row numbers only."""
    raise NotImplementedError

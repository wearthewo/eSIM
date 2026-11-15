"""Shared data types for the core pipeline.

ICCID, IMSI and MSISDN are always ``str``. They are never cast to int or float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning"]


class ParseError(Exception):
    """Raised when an upload cannot be read as a table.

    Covers unsupported extensions, bad encoding, missing required columns,
    numeric identifier cells in XLSX, and too many rows. Messages must never
    contain activation or confirmation codes.
    """


@dataclass(frozen=True, slots=True)
class EsimRecord:
    """One input row after parsing.

    ``activation_code`` is always the full LPA string, given directly or built
    from ``smdp_address`` + ``matching_id``. ``confirmation_code`` is a
    separate secret and is never part of ``activation_code``.
    """

    row_number: int
    iccid: str
    activation_code: str
    confirmation_code: str | None = None
    imsi: str | None = None
    msisdn: str | None = None
    customer_name: str | None = None
    plan_name: str | None = None

    def __repr__(self) -> str:
        """Return a repr with secrets redacted so they cannot leak via logs or tracebacks."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A problem with one field of one row.

    ``code`` is a stable machine identifier (e.g. ``"iccid_luhn"``).
    ``message`` is human-readable and never contains secret values.
    Only ``severity == "error"`` blocks ZIP generation.
    """

    row_number: int
    field: str
    code: str
    message: str
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Outcome of validating a batch of records."""

    valid: list[EsimRecord] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Issues with severity ``"error"``."""
        raise NotImplementedError

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Issues with severity ``"warning"``."""
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        """True when there are no errors (warnings are allowed)."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Branding:
    """Visual settings applied to every generated PDF."""

    company_name: str
    logo_png: bytes | None = None
    primary_hex: str = "#000000"
    footer_text: str = ""

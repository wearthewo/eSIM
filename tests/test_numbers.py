"""Numbers shown to people: thousands separators everywhere (app/core/numbers.py)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.batches import DailyUsage, quota_message
from app.core import parser
from app.core.models import ParseError
from app.core.numbers import thousands
from app.core.validator import validate
from app.web.templating import templates
from tests.conftest import synthetic_csv


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (7, "7"),
        (999, "999"),
        (1_000, "1,000"),
        (10_000, "10,000"),
        (1_234_567, "1,234,567"),
        (-2_500, "-2,500"),
    ],
)
def test_thousands(value: int, text: str) -> None:
    assert thousands(value) == text


def test_templates_use_the_same_helper() -> None:
    assert templates.env.filters["thousands"] is thousands
    rendered = templates.env.from_string("{{ n | thousands }} rows").render(n=10_000)
    assert rendered == "10,000 rows"


def test_too_many_rows_message() -> None:
    with pytest.raises(ParseError) as info:
        parser.parse_table(synthetic_csv(1_001), "big.csv", max_rows=1_000)
    assert str(info.value) == "Too many rows: the limit is 1,000 data rows per upload."


def test_duplicate_message_formats_the_row_number() -> None:
    lines = synthetic_csv(1_200).decode().splitlines()
    lines[1_100] = lines[1_050]  # row 1,101 repeats row 1,051 (row 1 is the header)
    records = parser.parse_csv(("\n".join(lines) + "\n").encode())
    messages = {issue.message for issue in validate(records).issues}
    assert "Duplicate ICCID; first seen in row 1,051." in messages
    assert "Duplicate activation code; first seen in row 1,051." in messages


def test_quota_message_formats_both_numbers() -> None:
    now = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)
    message = quota_message(DailyUsage(used=12_345, quota=20_000), now)
    assert message.startswith("Daily limit reached: 12,345 of 20,000 rows used today.")

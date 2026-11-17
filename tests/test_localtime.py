"""Greek time (Europe/Athens): display, strict dd/mm/yyyy and HH:mm input, the History range.

In 2026 the clocks go forward on 29 March at 01:00 UTC (03:00 EET -> 04:00 EEST) and back
on 25 October at 01:00 UTC (04:00 EEST -> 03:00 EET).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.localtime import (
    MSG_FROM_AFTER_TO,
    DateInputError,
    default_fields,
    format_athens,
    local_to_utc,
    parse_date,
    parse_range,
    parse_time,
)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


# --- Display -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "shown"),
    [
        (utc(2026, 1, 15, 10, 0), "15/01/2026 12:00"),  # winter, UTC+2
        (utc(2026, 7, 15, 10, 0), "15/07/2026 13:00"),  # summer, UTC+3
        (utc(2026, 3, 29, 0, 59), "29/03/2026 02:59"),  # just before the spring change
        (utc(2026, 3, 29, 1, 0), "29/03/2026 04:00"),  # 03:00 local is skipped
        (utc(2026, 10, 25, 0, 59), "25/10/2026 03:59"),  # first 03:59 (summer time)
        (utc(2026, 10, 25, 1, 0), "25/10/2026 03:00"),  # back to 03:00 (winter time)
        (utc(2026, 12, 31, 22, 30), "01/01/2027 00:30"),  # the local date is the next day
    ],
)
def test_format_athens(moment: dt.datetime, shown: str) -> None:
    assert format_athens(moment) == shown


def test_format_athens_needs_an_aware_time() -> None:
    assert format_athens(None) == "—"
    with pytest.raises(ValueError):
        format_athens(dt.datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001


# --- Input ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("05/03/2026", dt.date(2026, 3, 5)),
        (" 31/12/2026 ", dt.date(2026, 12, 31)),
        ("29/02/2028", dt.date(2028, 2, 29)),
    ],
)
def test_parse_date(text: str, expected: dt.date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5/3/2026",
        "05/03/26",
        "2026-03-05",
        "05-03-2026",
        "05.03.2026",
        "05/03/2026x",
        "05/03/2026 10:00",
        "٠٥/٠٣/٢٠٢٦",  # Arabic-Indic digits
        "０５/０３/２０２６",  # full-width digits
    ],
)
def test_parse_date_rejects_other_formats(text: str) -> None:
    with pytest.raises(DateInputError, match=r"Enter the To date as dd/mm/yyyy"):
        parse_date(text, "To")


@pytest.mark.parametrize("text", ["31/02/2026", "29/02/2026", "00/01/2026", "01/13/2026"])
def test_parse_date_rejects_dates_that_do_not_exist(text: str) -> None:
    with pytest.raises(DateInputError, match=f"The From date {text} does not exist"):
        parse_date(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("00:00", dt.time(0, 0)), ("09:30", dt.time(9, 30)), ("23:59", dt.time(23, 59)), ("", None)],
)
def test_parse_time(text: str, expected: dt.time | None) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text",
    ["24:00", "09:60", "9:30", "0930", "09:30:00", "09.30", "9am", "٠٩:٣٠"],
)
def test_parse_time_rejects_other_formats(text: str) -> None:
    with pytest.raises(DateInputError, match=r"Enter the From time as HH:mm \(24-hour\)"):
        parse_time(text)


def test_local_to_utc_around_the_clock_changes() -> None:
    assert local_to_utc(dt.date(2026, 1, 15), dt.time(12, 0)) == utc(2026, 1, 15, 10, 0)
    assert local_to_utc(dt.date(2026, 7, 15), dt.time(13, 0)) == utc(2026, 7, 15, 10, 0)
    # 03:30 on 25 October happens twice: fold 0 is the first (summer time), fold 1 the second.
    october = dt.date(2026, 10, 25)
    assert local_to_utc(october, dt.time(3, 30), fold=0) == utc(2026, 10, 25, 0, 30)
    assert local_to_utc(october, dt.time(3, 30), fold=1) == utc(2026, 10, 25, 1, 30)
    # 03:30 on 29 March does not exist; it is read with the offset before the change.
    assert local_to_utc(dt.date(2026, 3, 29), dt.time(3, 30)) == utc(2026, 3, 29, 1, 30)
    assert local_to_utc(dt.date(2026, 3, 29), dt.time(4, 0)) == utc(2026, 3, 29, 1, 0)


# --- The History range ------------------------------------------------------------------------

NOW = utc(2026, 11, 17, 10, 15)  # 12:15 in Athens


def test_defaults_are_thirty_days_ago_until_now() -> None:
    assert default_fields(NOW) == {
        "from_date": "18/10/2026",
        "from_time": "00:00",
        "to_date": "17/11/2026",
        "to_time": "12:15",
    }
    window = parse_range(None, None, None, None, now=NOW)
    assert window.start == utc(2026, 10, 17, 21, 0)  # 18 October was still summer time
    assert window.end == NOW.replace(minute=16)  # the current minute is included
    assert (window.from_date, window.from_time, window.to_date, window.to_time) == (
        "18/10/2026",
        "00:00",
        "17/11/2026",
        "12:15",
    )
    assert parse_range("", "", "", "", now=NOW) == window  # empty fields: the defaults


def test_to_without_a_time_covers_the_whole_day() -> None:
    window = parse_range("01/11/2026", "", "02/11/2026", "", now=NOW)
    assert window.start == utc(2026, 10, 31, 22, 0)
    assert window.end == utc(2026, 11, 2, 22, 0)  # 03/11/2026 00:00 Athens
    assert window.from_time == "00:00" and window.to_time == ""


def test_to_with_a_time_covers_that_minute() -> None:
    window = parse_range("01/07/2026", "08:00", "01/07/2026", "08:00", now=NOW)
    assert (window.start, window.end) == (utc(2026, 7, 1, 5, 0), utc(2026, 7, 1, 5, 1))


def test_range_over_the_repeated_october_hour_includes_both() -> None:
    window = parse_range("25/10/2026", "03:00", "25/10/2026", "03:59", now=NOW)
    assert window.start == utc(2026, 10, 25, 0, 0)  # the first 03:00 (summer time)
    assert window.end == utc(2026, 10, 25, 2, 0)  # after the second 03:59 (winter time)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (("02/11/2026", "", "01/11/2026", ""), MSG_FROM_AFTER_TO),
        (("01/11/2026", "10:01", "01/11/2026", "10:00"), MSG_FROM_AFTER_TO),
        (("2026-11-01", "", "02/11/2026", ""), "Enter the From date as dd/mm/yyyy"),
        (("01/11/2026", "", "31/11/2026", ""), "The To date 31/11/2026 does not exist"),
        (("01/11/2026", "25:00", "02/11/2026", ""), "Enter the From time as HH:mm"),
        (("01/11/2026", "", "02/11/2026", "7pm"), "Enter the To time as HH:mm"),
    ],
)
def test_bad_ranges(fields: tuple[str, str, str, str], message: str) -> None:
    with pytest.raises(DateInputError, match=message):
        parse_range(*fields, now=NOW)

"""Greek local time (Europe/Athens) for what users read and type: dd/mm/yyyy HH:mm, 24-hour.

The database stores UTC (``timestamptz``); conversion happens only here, at the edges.
``zoneinfo`` needs a time zone database: the system one on Linux (Render), the
``tzdata`` package on Windows (installed with psycopg). ``ATHENS`` is loaded at import,
so a missing database stops the app at startup instead of on the first page view.

Input is strict: two-digit day and month, four-digit year, two-digit hour and minute,
ASCII digits only, and a real calendar date. Messages are safe to show as they are.

Ambiguous and missing local times (the clock change on the last Sunday of March and of
October): a "From" time takes the earlier of two possible instants (fold 0) and a "To"
time the later (fold 1), so a range always covers the whole repeated hour. A time that
does not exist (03:00-03:59 on the March change day) is read with the offset before the
change, as ``zoneinfo`` does.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")

DATE_FORMAT = "%d/%m/%Y"
TIME_FORMAT = "%H:%M"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"
DEFAULT_RANGE_DAYS = 30

_DATE = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
_TIME = re.compile(r"([0-9]{2}):([0-9]{2})")

MSG_BAD_DATE = "Enter the {label} date as dd/mm/yyyy, for example 05/03/2026."
MSG_NO_SUCH_DATE = "The {label} date {value} does not exist."
MSG_BAD_TIME = "Enter the {label} time as HH:mm (24-hour), for example 09:30, or leave it empty."
MSG_FROM_AFTER_TO = "The From date and time must be before the To date and time."


class DateInputError(ValueError):
    """A date or time field could not be read. ``str(exc)`` is safe to show."""


def to_athens(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    return value.astimezone(ATHENS)


def format_athens(value: dt.datetime | None) -> str:
    """``dd/mm/yyyy HH:mm`` in Greek time; a dash for none."""
    if value is None:
        return "—"
    return to_athens(value).strftime(DATETIME_FORMAT)


def daily_reset_athens(now: dt.datetime) -> str:
    """When the UTC day after ``now`` starts, as Greek ``HH:mm``: 02:00 in winter (EET),
    03:00 in summer (EEST). Daily quotas (the ``usage`` table) count UTC days."""
    utc = now.astimezone(dt.UTC)
    next_midnight = dt.datetime.combine(utc.date() + dt.timedelta(days=1), dt.time(0), dt.UTC)
    return to_athens(next_midnight).strftime(TIME_FORMAT)


def parse_date(value: str, label: str = "From") -> dt.date:
    """A ``dd/mm/yyyy`` field. Raises ``DateInputError``."""
    text = value.strip()
    match = _DATE.fullmatch(text)
    if match is None:
        raise DateInputError(MSG_BAD_DATE.format(label=label))
    day, month, year = (int(part) for part in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        raise DateInputError(MSG_NO_SUCH_DATE.format(label=label, value=text)) from None


def parse_time(value: str, label: str = "From") -> dt.time | None:
    """A ``HH:mm`` field (24-hour); ``None`` when empty. Raises ``DateInputError``."""
    text = value.strip()
    if not text:
        return None
    match = _TIME.fullmatch(text)
    if match is None:
        raise DateInputError(MSG_BAD_TIME.format(label=label))
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise DateInputError(MSG_BAD_TIME.format(label=label))
    return dt.time(hour, minute)


def local_to_utc(day: dt.date, time: dt.time, *, fold: int = 0) -> dt.datetime:
    """The UTC instant of a Greek wall-clock time (see the module docstring for ``fold``)."""
    local = dt.datetime.combine(day, time, tzinfo=ATHENS).replace(fold=fold)
    return local.astimezone(dt.UTC)


@dataclass(frozen=True, slots=True)
class HistoryRange:
    """A From/To filter: batches with ``start <= created_at < end`` (both UTC).

    The ``*_text`` fields are the values to show in the form (normalised when valid).
    """

    start: dt.datetime
    end: dt.datetime
    from_date: str
    from_time: str
    to_date: str
    to_time: str


def default_fields(now: dt.datetime) -> dict[str, str]:
    """From = 30 days ago 00:00, To = now (Greek time)."""
    local = to_athens(now)
    start = local.date() - dt.timedelta(days=DEFAULT_RANGE_DAYS)
    return {
        "from_date": start.strftime(DATE_FORMAT),
        "from_time": "00:00",
        "to_date": local.strftime(DATE_FORMAT),
        "to_time": local.strftime(TIME_FORMAT),
    }


def parse_range(
    from_date: str | None,
    from_time: str | None,
    to_date: str | None,
    to_time: str | None,
    *,
    now: dt.datetime,
) -> HistoryRange:
    """Read the History filter. An absent or empty date takes the default (with the
    default time, unless a time was given).

    From without a time starts at 00:00. To without a time covers the whole day; To
    with a time covers that whole minute. Raises ``DateInputError``.
    """
    defaults = default_fields(now)
    if not (from_date or "").strip():
        from_date, from_time = defaults["from_date"], from_time or defaults["from_time"]
    if not (to_date or "").strip():
        to_date, to_time = defaults["to_date"], to_time or defaults["to_time"]

    start_day = parse_date(from_date, "From")
    start_time = parse_time(from_time or "", "From") or dt.time(0, 0)
    end_day = parse_date(to_date, "To")
    end_time = parse_time(to_time or "", "To")

    start = local_to_utc(start_day, start_time, fold=0)
    if end_time is None:
        end = local_to_utc(end_day + dt.timedelta(days=1), dt.time(0, 0), fold=0)
    else:
        end = local_to_utc(end_day, end_time, fold=1) + dt.timedelta(minutes=1)
    if start >= end:
        raise DateInputError(MSG_FROM_AFTER_TO)
    return HistoryRange(
        start=start,
        end=end,
        from_date=start_day.strftime(DATE_FORMAT),
        from_time=start_time.strftime(TIME_FORMAT),
        to_date=end_day.strftime(DATE_FORMAT),
        to_time=end_time.strftime(TIME_FORMAT) if end_time is not None else "",
    )

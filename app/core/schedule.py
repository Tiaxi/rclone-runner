"""Cron schedules: their weekday field, and a short description of them for the job pages.

Schedules use standard cron weekday numbers, 0 or 7 for Sunday and 1 for Monday. APScheduler
counts from 0 for Monday and steps through its own order (``*/2`` there is Monday, Wednesday,
Friday, Sunday), so the scheduler gets the weekdays as names, from cron_weekdays.
"""

from __future__ import annotations

import re

# Cron weekday numbers (0 = Sunday) to names, in APScheduler's spelling.
WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_FULL_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
# Weekdays in the order the job pages list them, Monday first.
_DISPLAY_ORDER = (1, 2, 3, 4, 5, 6, 0)
_STEP = re.compile(r"^(?P<range>[^/]+)(?:/(?P<step>[0-9]+))?$")


def normalize_cron(expression: str) -> str:
    return " ".join(expression.split())


def cron_weekdays(field: str) -> frozenset[int]:
    """The cron weekday numbers (0 = Sunday to 6 = Saturday) a weekday field selects.

    Takes numbers 0-7 (7 is Sunday too), three-letter names, ranges, lists and steps, as cron
    does: ``1-5``, ``mon-fri``, ``*/2``, ``0,6``, ``1-5/2``. Raises ValueError for anything else.
    """
    days: set[int] = set()
    for part in field.lower().split(","):
        match = _STEP.match(part)
        if match is None:
            raise ValueError(f"{field!r} is not a weekday field")
        step = int(match["step"]) if match["step"] is not None else 1
        if step < 1:
            raise ValueError(f"the step in {part!r} must be at least 1")
        first, last = _weekday_range(match["range"], part, has_step=match["step"] is not None)
        days.update(day % 7 for day in range(first, last + 1, step))
    return frozenset(days)


def _weekday_range(value: str, part: str, *, has_step: bool) -> tuple[int, int]:
    if value == "*":
        return 0, 6
    first_text, separator, last_text = value.partition("-")
    first = _weekday(first_text, part)
    if not separator:
        # As in cron, "1/2" steps from Monday to the end of the week.
        return (first, 6) if has_step else (first, first)
    last = _weekday(last_text, part)
    if last == 0 and first > 0:
        last = 7  # "fri-sun" and "5-0" end on the Sunday after.
    if last < first:
        raise ValueError(f"the weekday range {part!r} runs backwards")
    return first, last


def _weekday(value: str, part: str) -> int:
    if value in WEEKDAY_NAMES:
        return WEEKDAY_NAMES.index(value)
    if value.isascii() and value.isdigit() and int(value) <= 7:
        return int(value)
    raise ValueError(f"{part!r} is not a weekday: use 0-7 (0 and 7 are Sunday) or sun-sat")


def scheduler_weekdays(field: str) -> str:
    """The weekday field for APScheduler: "*", or the selected weekdays by name."""
    if field == "*":
        return "*"
    return ",".join(WEEKDAY_NAMES[day] for day in sorted(cron_weekdays(field)))


def restricts_day_and_weekday(day: str, day_of_week: str) -> bool:
    """Whether cron would run on either the day of the month or the weekday.

    cron runs on days matching either field when neither starts with "*"; APScheduler only on
    days matching both, so such a schedule would run on different days than it says.
    """
    return not day.startswith("*") and not day_of_week.startswith("*")


def cron_summary(expression: str) -> str:
    normalized = normalize_cron(expression)
    if not normalized:
        return "Never"

    parts = normalized.split()
    if len(parts) != 5:
        return f"Custom cron: {normalized}"

    minute, hour, day, month, day_of_week = parts
    if minute.startswith("*/") and hour == day == month == day_of_week == "*":
        return f"Every {minute[2:]} minutes"
    if minute == "0" and hour.startswith("*/") and day == month == day_of_week == "*":
        return f"Every {hour[2:]} hours"
    if day == month == day_of_week == "*":
        return f"Daily at {_time(hour, minute)}"
    if day == month == "*" and day_of_week != "*":
        days = _day_names(day_of_week)
        if days is None:
            return f"Custom cron: {normalized}"
        return f"Weekly on {days} at {_time(hour, minute)}"
    if month == day_of_week == "*" and day != "*":
        return f"Monthly on day {day} at {_time(hour, minute)}"
    return f"Custom cron: {normalized}"


def _time(hour: str, minute: str) -> str:
    if hour.isdigit() and minute.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return f"{hour}:{minute}"


def _day_names(field: str) -> str | None:
    """The weekdays of a weekday field in words, or None if it is not a valid one."""
    try:
        days = cron_weekdays(field)
    except ValueError:
        return None
    ordered = [day for day in _DISPLAY_ORDER if day in days]
    if len(ordered) == 7:
        return "every day"
    names = [_FULL_NAMES[day] for day in ordered]
    positions = [_DISPLAY_ORDER.index(day) for day in ordered]
    if len(names) >= 3 and positions == list(range(positions[0], positions[-1] + 1)):
        return f"{names[0]}–{names[-1]}"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"

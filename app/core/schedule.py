from __future__ import annotations


def normalize_cron(expression: str) -> str:
    return " ".join(expression.split())


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
        return f"Weekly on {_day_name(day_of_week)} at {_time(hour, minute)}"
    if month == day_of_week == "*" and day != "*":
        return f"Monthly on day {day} at {_time(hour, minute)}"
    return f"Custom cron: {normalized}"


def _time(hour: str, minute: str) -> str:
    if hour.isdigit() and minute.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return f"{hour}:{minute}"


def _day_name(value: str) -> str:
    names = {
        "0": "Sunday",
        "1": "Monday",
        "2": "Tuesday",
        "3": "Wednesday",
        "4": "Thursday",
        "5": "Friday",
        "6": "Saturday",
        "7": "Sunday",
        "sun": "Sunday",
        "mon": "Monday",
        "tue": "Tuesday",
        "wed": "Wednesday",
        "thu": "Thursday",
        "fri": "Friday",
        "sat": "Saturday",
    }
    return names.get(value.lower(), value)

from datetime import UTC, datetime

from app.main import _format_duration, _format_local_time


def test_format_local_time_converts_utc_to_configured_timezone():
    formatted = _format_local_time(datetime(2026, 1, 1, 12, 30, tzinfo=UTC))

    assert formatted == "2026-01-01 14:30:00 EET"


def test_format_local_time_treats_naive_database_values_as_utc():
    formatted = _format_local_time(datetime(2026, 7, 1, 12, 30))

    assert formatted == "2026-07-01 15:30:00 EEST"


def test_format_duration_uses_compact_units():
    assert (
        _format_duration(
            datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 26, 12, 0, 3, 250000, tzinfo=UTC),
        )
        == "3.2s"
    )
    assert (
        _format_duration(
            datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 26, 12, 2, 5, tzinfo=UTC),
        )
        == "2m 5s"
    )
    assert (
        _format_duration(
            datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 4, 26, 14, 3, 5, tzinfo=UTC),
        )
        == "2h 3m 5s"
    )

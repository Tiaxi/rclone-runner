from datetime import UTC, datetime

from app.db import ConsoleRunRecord
from app.main import _console_history_row, _console_log_path


def test_console_log_path_includes_microseconds_to_avoid_quick_run_collisions():
    first = _console_log_path(datetime(2026, 4, 26, 12, 0, 0, 1, tzinfo=UTC))
    second = _console_log_path(datetime(2026, 4, 26, 12, 0, 0, 2, tzinfo=UTC))

    assert first != second
    assert first.name == "20260426T120000000001Z.log"
    assert second.name == "20260426T120000000002Z.log"


def test_console_history_row_links_to_log_without_embedding_output(tmp_path):
    record = ConsoleRunRecord(
        id=7,
        command="version",
        argv_json='["rclone", "version"]',
        exit_code=0,
        log_path=str(tmp_path / "missing.log"),
        started_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 12, 0, 1, tzinfo=UTC),
    )

    row = _console_history_row(record)

    assert row == {
        "id": 7,
        "command": "version",
        "started_at": "2026-04-26 15:00:00 EEST",
        "exit_code": 0,
    }

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ConsoleRunRecord
from app.main import (
    _console_history_row,
    _console_log_path,
    _finish_console_run_record,
    _start_console_run_record,
)


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


def test_console_run_is_recorded_while_running_and_finished_later(tmp_path):
    session_factory = _session_factory(tmp_path / "runs.db")
    started_at = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    ended_at = datetime(2026, 4, 26, 12, 0, 1, tzinfo=UTC)
    with session_factory() as db:
        run_id = _start_console_run_record(
            db,
            "version",
            ["rclone", "version"],
            tmp_path / "console.log",
            started_at,
        )

        running = db.get(ConsoleRunRecord, run_id)
        assert running.status == "running"
        assert running.exit_code is None
        assert running.ended_at is None

        finished = _finish_console_run_record(db, run_id, 0, ended_at)

        assert finished.status == "success"
        assert finished.exit_code == 0
        assert finished.ended_at == ended_at.replace(tzinfo=None)


def _session_factory(database_path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

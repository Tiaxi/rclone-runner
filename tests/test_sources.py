import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.sources as sources
from app.core.commands import build_rclone_argv
from app.core.measure import SourceMeasurer
from app.core.sources import SizeCommand, parse_size_output, size_command
from app.db import Base, SourceMeasurementRecord

VALUE_FLAGS = frozenset(
    {"--backup-dir", "--transfers", "--exclude", "--combined", "--max-delete", "--bwlimit"}
)


@pytest.fixture
def known_flags(monkeypatch):
    monkeypatch.setattr(sources, "_flags_with_values", lambda subcommand: VALUE_FLAGS)


def _size(step: str, common: str = "") -> SizeCommand | None:
    return size_command(build_rclone_argv(step, common, {}))


def test_size_command_finds_the_source_past_flag_values(known_flags):
    command = _size(
        "sync /media/Musa crypt:current/Musa",
        "--backup-dir crypt:versions/2026-11-15/Musa --transfers 8 --exclude '*.tmp' -v",
    )

    assert command.source == "/media/Musa"
    # Only flags that decide which files count are kept.
    assert command.argv == ["rclone", "size", "--json", "--exclude", "*.tmp", "/media/Musa"]


def test_size_command_handles_terminators_and_flag_values_named_like_them(known_flags):
    assert _size("copy -- /src remote:dst").source == "/src"
    assert _size("copy /src remote:dst --backup-dir -- -v").source == "/src"
    assert _size("sync --combined report.txt /media/A remote:A", "--fast-list -L").argv == [
        "rclone",
        "size",
        "--json",
        "--fast-list",
        "-L",
        "/media/A",
    ]


def test_size_command_skips_commands_without_a_source(known_flags):
    assert _size("lsd remote:") is None
    assert _size("check /a remote:a") is None
    assert _size("sync --dry-run") is None


def test_size_command_key_follows_source_and_filters_only(known_flags):
    first = _size("sync /a r:a", "--backup-dir r:v/2026-11-15 --exclude x")
    next_day = _size("sync /a r:a", "--backup-dir r:v/2026-11-16 --exclude x")
    other_filter = _size("sync /a r:a", "--exclude y")

    assert first.key == next_day.key
    assert first.key != other_filter.key


def test_parse_size_output():
    result = parse_size_output('{"count":38201,"bytes":442721468416,"sizeless":0}')
    assert (result.files, result.bytes) == (38201, 442721468416)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_flag_list_and_size(tmp_path):
    sources._flags_with_values.cache_clear()
    source = tmp_path / "Musa"
    source.mkdir()
    (source / "a.flac").write_bytes(b"x" * 1000)
    (source / "b.tmp").write_bytes(b"x" * 500)

    command = size_command(
        build_rclone_argv(
            f"sync {source} remote:Musa",
            "--backup-dir remote:versions --transfers 8 --exclude *.tmp --create-empty-src-dirs",
            {},
        )
    )

    output = subprocess.run(command.argv, capture_output=True, text=True, check=True).stdout
    assert command.source == str(source)
    assert parse_size_output(output).files == 1
    assert parse_size_output(output).bytes == 1000


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _fake(key: str, script: str) -> SizeCommand:
    """A stand-in for rclone size: a Python one-liner, keyed like a real command."""
    return SizeCommand(source=f"/media/{key}", argv=[sys.executable, "-c", script])


def _record(session_factory, command: SizeCommand) -> SourceMeasurementRecord:
    with session_factory() as db:
        return db.query(SourceMeasurementRecord).filter_by(key=command.key).one()


async def test_measurer_saves_results_and_keeps_them_when_a_later_run_fails(tmp_path):
    session_factory = _session_factory(tmp_path / "runs.db")
    measurer = SourceMeasurer(session_factory)
    ok = _fake("a", 'print(\'{"count": 3, "bytes": 4096}\')')

    assert measurer.start([ok], {}) == 1
    assert _record(session_factory, ok).status == "running"
    await measurer.wait()
    record = _record(session_factory, ok)
    assert (record.status, record.files, record.bytes) == ("success", 3, 4096)
    measured_at = record.measured_at

    # Measure the same source again, this time with rclone failing: move the saved result
    # over to the failing command's key.
    failing = _fake("a", "import sys; sys.stderr.write('ERROR : directory not found\\n'); exit(3)")
    with session_factory() as db:
        db.query(SourceMeasurementRecord).filter_by(key=ok.key).update({"key": failing.key})
        db.commit()
    measurer.start([failing], {})
    await measurer.wait()
    record = _record(session_factory, failing)
    assert record.status == "failed"
    assert record.error == "ERROR : directory not found"
    # The last good result stays on show.
    assert (record.files, record.bytes, record.measured_at) == (3, 4096, measured_at)


async def test_measurer_skips_sources_already_being_measured_and_times_out(tmp_path):
    session_factory = _session_factory(tmp_path / "runs.db")
    measurer = SourceMeasurer(session_factory, timeout=0.2)
    slow = _fake("slow", "import time; time.sleep(5)")

    assert measurer.start([slow, slow], {}) == 1
    assert measurer.start([slow], {}) == 0
    await measurer.wait()

    record = _record(session_factory, slow)
    assert record.status == "failed"
    assert record.error.startswith("Timed out")


async def test_measurer_reports_unreadable_output_and_recovers_after_restart(tmp_path):
    session_factory = _session_factory(tmp_path / "runs.db")
    measurer = SourceMeasurer(session_factory)
    garbled = _fake("garbled", "print('not json')")
    measurer.start([garbled], {})
    await measurer.wait()
    assert "could not be read" in _record(session_factory, garbled).error

    stuck = _fake("stuck", "")
    with session_factory() as db:
        db.add(
            SourceMeasurementRecord(
                key=stuck.key,
                source=stuck.source,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        db.commit()
    SourceMeasurer(session_factory).recover_interrupted()
    record = _record(session_factory, stuck)
    assert record.status == "failed"
    assert "restarted" in record.error


def test_key_is_the_json_argv():
    command = SizeCommand(source="/a", argv=["rclone", "size", "--json", "/a"])
    assert json.loads(command.key) == command.argv

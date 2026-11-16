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
from app.core.sources import FlagSet, SizeCommand, parse_size_output, size_command
from app.db import Base, SourceMeasurementRecord


def _size(step: str, common: str = "", env=None) -> SizeCommand | None:
    return size_command(build_rclone_argv(step, common, {}), env)


def test_size_command_finds_the_source_past_flag_values():
    command = _size(
        "sync /media/Musa crypt:current/Musa",
        "--backup-dir crypt:versions/2026-11-15/Musa --transfers 8 --exclude '*.tmp' -v",
    )

    assert command.source == "/media/Musa"
    # Transfer and output flags are dropped; the filter stays; "--" guards the source.
    assert command.argv == ["rclone", "size", "--json", "--exclude", "*.tmp", "--", "/media/Musa"]


def test_size_command_handles_terminators_and_flag_values_named_like_them():
    assert _size("copy -- /src remote:dst").source == "/src"
    assert _size("copy /src remote:dst --backup-dir -- -v").source == "/src"
    # A source starting with "-" stays a path.
    assert _size("copy -- -src dest").argv[-2:] == ["--", "-src"]
    # Enum-valued flags take a value too.
    assert _size("copy --cutoff-mode HARD /src /dst").source == "/src"
    assert _size("sync --combined report.txt /media/A remote:A", "--fast-list -L").argv == [
        "rclone",
        "size",
        "--json",
        "--fast-list",
        "-L",
        "--",
        "/media/A",
    ]


def test_size_command_keeps_connection_settings_and_all_filters():
    command = _size(
        "copy --config /etc/custom.conf remote:src /dst",
        "--sftp-host box.example -f '- *.tmp' --rc --rc-addr :5572 --dry-run",
    )
    assert command.argv == [
        "rclone",
        "size",
        "--json",
        "--sftp-host",
        "box.example",
        "-f",
        "- *.tmp",
        "--config",
        "/etc/custom.conf",
        "--",
        "remote:src",
    ]
    # bisync's own filter file becomes the global equivalent rclone size understands.
    assert _size("bisync /a remote:b --filters-file /f.txt").argv[3:5] == [
        "--filter-from",
        "/f.txt",
    ]
    assert _size("bisync /a remote:b --filters-file=/f.txt").argv[3] == "--filter-from=/f.txt"


def test_size_command_skips_commands_without_a_source():
    assert _size("lsd remote:") is None
    assert _size("check /a remote:a") is None
    assert _size("sync --dry-run") is None


def test_size_command_key_follows_source_filters_and_rclone_environment():
    first = _size("sync /a r:a", "--backup-dir r:v/2026-11-15 --exclude x")
    next_day = _size("sync /a r:a", "--backup-dir r:v/2026-11-16 --exclude x")
    other_filter = _size("sync /a r:a", "--exclude y")
    other_remote = _size(
        "sync /a r:a", "--backup-dir r:v/2026-11-15 --exclude x", {"RCLONE_CONFIG_R_REMOTE": "/b"}
    )
    unrelated_env = _size(
        "sync /a r:a", "--backup-dir r:v/2026-11-15 --exclude x", {"BW_LIMIT": "8M"}
    )

    assert first.key == next_day.key == unrelated_env.key
    assert first.key != other_filter.key
    assert first.key != other_remote.key
    # Environment values may be secrets; only a digest goes into the key.
    assert "/b" not in other_remote.key


async def test_flag_lists_load_in_a_thread(monkeypatch):
    import threading

    threads = []

    def fake_read(command):
        threads.append(threading.current_thread())
        return FlagSet(frozenset(), frozenset())

    monkeypatch.setattr(sources, "_flag_sets", {})
    monkeypatch.setattr(sources, "_read_flags", fake_read)
    await sources.ensure_flags()

    assert len(threads) == 1 + len(sources.SOURCE_SUBCOMMANDS)
    assert threading.main_thread() not in threads
    await sources.ensure_flags()  # cached: no more reads
    assert len(threads) == 1 + len(sources.SOURCE_SUBCOMMANDS)


def test_parse_size_output():
    result = parse_size_output('{"count":38201,"bytes":442721468416,"sizeless":0}')
    assert (result.files, result.bytes) == (38201, 442721468416)


def test_size_command_keeps_filters_given_as_grouped_short_flags():
    # -f'- *.tmp' reaches rclone as the single argument "-f- *.tmp".
    assert _size("copy /src /dst", "-f'- *.tmp'").argv == [
        "rclone",
        "size",
        "--json",
        "-f",
        "- *.tmp",
        "--",
        "/src",
    ]
    # -f ends the group, so it takes the next argument, which must not pass for the source.
    command = _size("copy /src /dst", "-vf '- *.tmp'")
    assert command.source == "/src"
    assert command.argv == ["rclone", "size", "--json", "-f", "- *.tmp", "--", "/src"]
    assert _size("copy /src /dst", "-Lf='+ *.flac'").argv == [
        "rclone",
        "size",
        "--json",
        "-L",
        "-f=+ *.flac",
        "--",
        "/src",
    ]
    # A letter after "=" is part of the value; -n is dropped, -L kept.
    assert _size("copy /src /dst", "-Ln=false").argv == [
        "rclone",
        "size",
        "--json",
        "-L",
        "--",
        "/src",
    ]


def test_split_command_reads_short_flag_groups_as_rclone_does():
    def flags(*args):
        return sources.split_command(["rclone", "sync", *args, "/a", "r:a"])

    assert flags("-vn") == ([["-v"], ["-n"]], ["/a", "r:a"])
    assert flags("-vn=false") == ([["-v"], ["-n=false"]], ["/a", "r:a"])
    assert flags("-vfx") == ([["-v"], ["-f", "x"]], ["/a", "r:a"])
    assert flags("-f", "- *.tmp") == ([["-f", "- *.tmp"]], ["/a", "r:a"])
    # A letter rclone doesn't list: the rest of the group stays together, unread.
    assert flags("-vZf") == ([["-v"], ["-Zf"]], ["/a", "r:a"])


@pytest.mark.real_rclone
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
@pytest.mark.parametrize("common", ["-f'- *.tmp'", "-vf '- *.tmp'", "-f='- *.tmp'"])
def test_real_rclone_size_keeps_grouped_short_filters(tmp_path, common):
    sources.load_flags()
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"x" * 5)
    (source / "omit.tmp").write_bytes(b"x" * 100)

    command = size_command(build_rclone_argv(f"copy {source} {tmp_path / 'dst'}", common, {}))

    output = subprocess.run(command.argv, capture_output=True, text=True, check=True).stdout
    assert command.source == str(source)
    assert parse_size_output(output) == sources.SourceSize(bytes=5, files=1)


@pytest.mark.real_rclone
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_real_rclone_flag_list_and_size(tmp_path):
    sources.load_flags()
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


def test_key_holds_the_argv_and_environment_digest():
    command = SizeCommand(source="/a", argv=["rclone", "size", "--json", "/a"], env_digest="ab")
    assert json.loads(command.key) == {"argv": command.argv, "env": "ab"}


def test_size_command_drops_flags_that_fail_size_or_write_files():
    command = _size(
        "copy /src remote:dst --error-on-no-transfer",
        "--cpuprofile /logs/backup.cpu --memprofile /logs/backup.mem",
    )
    assert command.argv == ["rclone", "size", "--json", "--", "/src"]


async def test_failed_flag_reads_back_off_and_never_block_requests(monkeypatch):
    calls = []

    def failing_read(command):
        calls.append(command)
        return None

    clock = [1000.0]
    monkeypatch.setattr(sources, "_flag_sets", {})
    monkeypatch.setattr(sources, "_failed_at", {})
    monkeypatch.setattr(sources, "_read_flags", failing_read)
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])

    await sources.ensure_flags()
    reads = len(sources.SOURCE_SUBCOMMANDS) + 1
    assert len(calls) == reads

    # Request code only reads the cache: no source without the flag list, and no rclone call.
    assert _size("sync /a r:a", "--exclude *.tmp") is None
    assert sources.split_command(["rclone", "sync", "-n", "/a", "r:a"])[0] == [["-n"]]
    await sources.ensure_flags()  # within the back-off
    assert len(calls) == reads

    clock[0] += sources.FLAG_RETRY_SECONDS
    await sources.ensure_flags()
    assert len(calls) == 2 * reads

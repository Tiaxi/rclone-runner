import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.notifications import read_log_tail
from app.core.rclone_stats import (
    STATS_TAIL_BYTES,
    STATS_UNAVAILABLE_JSON,
    RcloneTransferStats,
    live_progress_from_log,
    parse_rclone_transfer_stats,
    run_stats_display,
    stats_from_log,
    stats_to_json,
    step_stats_display,
    step_stats_from_log,
    step_transfer_stats,
)

DRY_RUN_SUMMARY = """
Transferred:      782.816 MiB / 782.816 MiB, 100%, 12.345 MiB/s, ETA 0s
Checks:              112 / 112, 100%
Deleted:              16 (files), 0 (dirs), 0 B (freed)
Transferred:         134 / 134, 100%
Elapsed time:        1.2s
"""


ZERO_SUMMARY = """
Transferred:              0 B / 0 B, -, 0 B/s, ETA -
Checks:                   0 / 0, -
Transferred:              0 / 0, -
Elapsed time:         0.0s
"""

ZERO_SUMMARY_WITHOUT_FILE_LINE = """
Transferred:              0 B / 0 B, -, 0 B/s, ETA -
Checks:                5066 / 5066, 100%, Listed 10548
Elapsed time:         17.4s
"""


def test_parse_rclone_final_summary_with_transfers_and_deleted_files():
    assert parse_rclone_transfer_stats(DRY_RUN_SUMMARY) == RcloneTransferStats(
        transferred_bytes=820842070,
        transferred_files=134,
        deleted_files=16,
    )


def test_parse_rclone_zero_transfer_summary_without_delete_line():
    assert parse_rclone_transfer_stats(ZERO_SUMMARY) == RcloneTransferStats(
        transferred_bytes=0,
        transferred_files=0,
        deleted_files=0,
    )


def test_parse_rclone_zero_transfer_summary_without_file_transfer_line():
    assert parse_rclone_transfer_stats(ZERO_SUMMARY_WITHOUT_FILE_LINE) == RcloneTransferStats(
        transferred_bytes=0,
        transferred_files=0,
        deleted_files=0,
    )


def test_parse_rclone_stats_returns_none_without_summary_block():
    assert parse_rclone_transfer_stats("copying file\nno final totals here\n") is None


def test_stats_from_log_reads_final_stats_from_the_end_of_large_logs(tmp_path: Path):
    log_path = tmp_path / "step.log"
    filler = "INFO  : some/file.flac: Copied (new)\n" * (STATS_TAIL_BYTES // 30)
    log_path.write_text(ZERO_SUMMARY + filler + DRY_RUN_SUMMARY, encoding="utf-8")

    stats = stats_from_log(log_path)

    assert stats is not None
    assert stats.transferred_files == 134


def test_stats_from_log_ignores_stats_before_the_tail(tmp_path: Path):
    log_path = tmp_path / "step.log"
    filler = "INFO  : some/file.flac: Copied (new)\n" * (STATS_TAIL_BYTES // 30)
    log_path.write_text(DRY_RUN_SUMMARY + filler, encoding="utf-8")

    assert stats_from_log(log_path) is None


def test_stored_unavailable_marker_skips_log_parsing(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text(DRY_RUN_SUMMARY, encoding="utf-8")
    step_run = SimpleNamespace(
        transfer_stats_json=STATS_UNAVAILABLE_JSON,
        ended_at=object(),
        log_path=str(log_path),
        argv_json='["rclone", "sync", "a", "b"]',
    )
    legacy_step_run = SimpleNamespace(
        transfer_stats_json=None,
        ended_at=object(),
        log_path=str(log_path),
        argv_json='["rclone", "sync", "a", "b"]',
    )

    assert step_transfer_stats(step_run) is None
    assert step_transfer_stats(legacy_step_run).transferred_files == 134


def test_missing_stats_are_not_available_whatever_the_status():
    # rclone prints no totals without -v (or --stats-log-level NOTICE), even after copying.
    assert step_stats_display(None, "success").label == "Not available"
    assert step_stats_display(None).label == "Not available"
    assert step_stats_display(None, "failed").label == "Not available"
    assert step_stats_display(None, "canceled").transferred_data_label == "Not available"


def test_run_totals_say_when_some_steps_had_no_stats():
    known = RcloneTransferStats(transferred_bytes=2048, transferred_files=1, deleted_files=0)
    zero = RcloneTransferStats(transferred_bytes=0, transferred_files=0, deleted_files=0)

    complete = run_stats_display([known, zero])
    assert not complete.has_unavailable
    assert complete.transferred_data_label == "2 KiB"

    partial = run_stats_display([known, None])
    assert partial.has_unavailable
    assert partial.transferred_data_label == "at least 2 KiB"
    assert partial.transferred_files_label == "at least 1"

    unknown = run_stats_display([None, None])
    assert unknown.has_unavailable
    assert unknown.transferred_data_label == "Not available"
    assert run_stats_display([]).transferred_data_label == "0 B"

    # Steps still running: their totals are still to come.
    assert run_stats_display([], running=1).transferred_data_label == "Pending"
    assert run_stats_display([known], running=1).transferred_data_label == "at least 2 KiB"


def test_read_log_tail_returns_last_lines_of_large_log(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text("".join(f"line {index}\n" for index in range(200_000)))

    tail = read_log_tail(log_path, 3)

    assert tail == "line 199997\nline 199998\nline 199999\n"


def test_stats_from_log_keeps_first_tail_line_at_exact_line_boundary(tmp_path: Path):
    log_path = tmp_path / "step.log"
    summary = DRY_RUN_SUMMARY.lstrip("\n").encode()
    filler = b"." * (STATS_TAIL_BYTES - len(summary) - 1) + b"\n"
    # The last STATS_TAIL_BYTES start exactly at the "Transferred: ... MiB" line.
    log_path.write_bytes(b"earlier output\n" + summary + filler)

    stats = stats_from_log(log_path)

    assert stats is not None
    assert stats.transferred_files == 134


# Captured from rclone v1.75.2 with -v --stats 1s, in both output formats.
LIVE_BLOCK_LOG = (
    "Transferred:   \t    2.028 MiB / 5.723 MiB, 35%, 2.028 MiB/s, ETA 1s\n"
    "Transferred:            1 / 2, 50%\n"
    "Elapsed time:         1.0s\n"
    "2026/11/15 03:14:19 INFO  : big.bin: Copied (new)\n"
    "Transferred:   \t    4.028 MiB / 5.723 MiB, 70%, 2.028 MiB/s, ETA 0s\n"
    "Transferred:            1 / 2, 50%\n"
)
LIVE_ONE_LINE_LOG = (
    "2026/11/15 03:14:18 INFO  : small.bin: Copied (new)\n"
    "2026/11/15 03:14:19 INFO  :     2.028 MiB / 5.723 MiB, 35%, 2.028 MiB/s, ETA 1s\n"
)


def test_live_progress_uses_latest_report_in_block_format(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text(LIVE_BLOCK_LOG, encoding="utf-8")

    progress = live_progress_from_log(log_path)

    assert progress is not None
    assert progress.label == "4.028 MiB / 5.723 MiB (70%) · 2.028 MiB/s · ETA 0s"


def test_live_progress_reads_one_line_format(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text(LIVE_ONE_LINE_LOG, encoding="utf-8")

    assert live_progress_from_log(log_path).label == (
        "2.028 MiB / 5.723 MiB (35%) · 2.028 MiB/s · ETA 1s"
    )


def test_live_progress_omits_unknown_percent_and_eta(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text("Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -\n")

    assert live_progress_from_log(log_path).label == "0 B / 0 B · 0 B/s"


def test_live_progress_is_none_without_reports(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text("2026/11/15 03:14:18 INFO  : small.bin: Copied (new)\n")

    assert live_progress_from_log(log_path) is None
    assert live_progress_from_log(tmp_path / "missing.log") is None


def test_live_progress_reads_bit_units(tmp_path: Path):
    # Captured from rclone v1.75.2 with --stats-unit bits.
    log_path = tmp_path / "step.log"
    log_path.write_text(
        "Transferred:   \t    2.027 MiB / 5.722 MiB, 35%, 16.217 Mibit/s, ETA 1s\n",
        encoding="utf-8",
    )

    assert live_progress_from_log(log_path).label == (
        "2.027 MiB / 5.722 MiB (35%) · 16.217 Mibit/s · ETA 1s"
    )


def test_live_progress_keeps_last_report_when_lots_is_logged_after_it(tmp_path: Path):
    log_path = tmp_path / "step.log"
    log_path.write_text(LIVE_BLOCK_LOG, encoding="utf-8")
    assert live_progress_from_log(log_path) is not None

    copied = "2026/11/15 03:15:00 INFO  : NES/game.nes: Copied (new)\n"
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(copied * (2 * 1024 * 1024 // len(copied)))

    progress = live_progress_from_log(log_path)
    assert progress is not None
    assert progress.label == "4.028 MiB / 5.723 MiB (70%) · 2.028 MiB/s · ETA 0s"


def test_live_progress_waits_for_a_complete_line(tmp_path: Path):
    log_path = tmp_path / "step.log"
    line = "Transferred:   \t    2.028 MiB / 5.723 MiB, 35%, 2.028 MiB/s, ETA 1s"
    log_path.write_text(line[:30], encoding="utf-8")
    assert live_progress_from_log(log_path) is None

    log_path.write_text(line + "\n", encoding="utf-8")
    assert live_progress_from_log(log_path).percent == "35%"


def test_step_stats_display_reports_zero_totals_as_nothing_transferred():
    zero = RcloneTransferStats(transferred_bytes=0, transferred_files=0, deleted_files=0)

    # Not "No changes": rclone doesn't count folders made or modification times updated.
    assert step_stats_display(zero, "success").label == "Nothing transferred"
    # A failed step that printed zero totals did report them; keep the numbers.
    assert step_stats_display(zero, "failed").label == "0 B, 0 files, 0 deleted"


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
def test_a_copy_without_verbose_logging_is_not_reported_as_no_changes(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.touch()
    log_path = tmp_path / "step.log"
    with log_path.open("wb") as log_file:
        subprocess.run(
            ["rclone", "copy", "--config", str(config), str(source), str(tmp_path / "dst")],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=True,
        )

    assert sorted(path.name for path in (tmp_path / "dst").iterdir()) == ["a.txt", "b.txt"]
    stats = stats_from_log(log_path)
    assert stats is None
    assert step_stats_display(stats, "success").label == "Not available"


def test_totals_of_zero_from_a_move_are_not_taken_as_no_changes(tmp_path: Path):
    # A move to a destination that doesn't exist yet may just rename the source directory,
    # which rclone counts as nothing transferred.
    log_path = tmp_path / "step.log"
    log_path.write_text(
        "Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -\nElapsed time:  0.0s\n",
        encoding="utf-8",
    )
    assert step_stats_from_log(["rclone", "sync", "a", "b"], log_path).transferred_bytes == 0
    assert step_stats_from_log(["rclone", "move", "a", "b"], log_path) is None
    assert step_stats_from_log(["rclone", "moveto", "a", "b"], log_path) is None
    legacy = SimpleNamespace(
        transfer_stats_json=None,
        ended_at=object(),
        log_path=str(log_path),
        argv_json='["rclone", "move", "a", "b"]',
    )
    assert step_transfer_stats(legacy) is None
    # Totals stored before this check was made are read the same way.
    stored = SimpleNamespace(
        transfer_stats_json=stats_to_json(RcloneTransferStats(0, 0, 0)),
        ended_at=object(),
        log_path=str(log_path),
        argv_json='["rclone", "move", "--dry-run", "a", "b"]',
    )
    assert step_transfer_stats(stored) is None
    stored.argv_json = '["rclone", "sync", "a", "b"]'
    assert step_transfer_stats(stored).transferred_bytes == 0


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
@pytest.mark.parametrize("existing_destination", [False, True])
async def test_a_moved_directory_is_not_reported_as_no_changes(tmp_path, existing_destination):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.models import Job, JobStep
    from app.core.runner import LiveJobRunner
    from app.db import Base, JobStepRunRecord

    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "sub" / "b.txt").write_text("b", encoding="utf-8")
    destination = tmp_path / "dst"
    if existing_destination:
        destination.mkdir()
    config = tmp_path / "rclone.conf"
    config.touch()
    job = Job(
        id=1,
        name="move",
        common_args=f"--config {config}",
        env={},
        steps=[JobStep(id=1, name="move", command=f"move {source} {destination}")],
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)

    assert sorted(path.name for path in destination.iterdir()) == ["a.txt", "sub"]
    with session_factory() as db:
        step_run = db.get(JobStepRunRecord, first.id)
        stats = step_transfer_stats(step_run)
    if existing_destination:
        # Moved file by file, and counted.
        assert stats.transferred_files == 2
    else:
        # Renamed in one go, which rclone doesn't count: no claim of no changes.
        assert stats is None
        assert step_stats_display(stats, "success").label == "Not available"

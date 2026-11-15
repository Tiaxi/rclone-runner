from pathlib import Path
from types import SimpleNamespace

from app.core.notifications import read_log_tail
from app.core.rclone_stats import (
    STATS_TAIL_BYTES,
    STATS_UNAVAILABLE_JSON,
    RcloneTransferStats,
    live_progress_from_log,
    parse_rclone_transfer_stats,
    stats_from_log,
    step_stats_display,
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
        transfer_stats_json=STATS_UNAVAILABLE_JSON, ended_at=object(), log_path=str(log_path)
    )
    legacy_step_run = SimpleNamespace(
        transfer_stats_json=None, ended_at=object(), log_path=str(log_path)
    )

    assert step_transfer_stats(step_run) is None
    assert step_transfer_stats(legacy_step_run).transferred_files == 134


def test_missing_stats_label_depends_on_step_status():
    assert step_stats_display(None, "success").label == "No changes"
    assert step_stats_display(None).label == "No changes"
    assert step_stats_display(None, "failed").label == "Not available"
    assert step_stats_display(None, "canceled").transferred_data_label == "Not available"


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

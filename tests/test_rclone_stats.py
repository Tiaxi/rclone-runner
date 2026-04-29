from app.core.rclone_stats import RcloneTransferStats, parse_rclone_transfer_stats

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


def test_parse_rclone_stats_returns_none_without_summary_block():
    assert parse_rclone_transfer_stats("copying file\nno final totals here\n") is None

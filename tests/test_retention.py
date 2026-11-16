import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.retention import open_files, prune_logs


def test_prunes_old_logs_but_keeps_recent_logs():
    now = datetime(2026, 4, 26, tzinfo=UTC)

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        old_log = root / "old.log"
        recent_log = root / "recent.log"
        missing_log = root / "missing.log"
        old_log.write_text("old", encoding="utf-8")
        recent_log.write_text("recent", encoding="utf-8")

        deleted = prune_logs(
            [
                (old_log, now - timedelta(days=31)),
                (recent_log, now - timedelta(days=2)),
                (missing_log, now - timedelta(days=60)),
            ],
            keep_days=30,
            now=now,
        )

        assert deleted == 1
        assert not old_log.exists()
        assert recent_log.exists()


def test_prune_logs_treats_naive_database_timestamps_as_utc():
    now = datetime(2026, 4, 26, tzinfo=UTC)

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        old_log = root / "old.log"
        recent_log = root / "recent.log"
        old_log.write_text("old", encoding="utf-8")
        recent_log.write_text("recent", encoding="utf-8")

        deleted = prune_logs(
            [
                (old_log, datetime(2026, 3, 1)),
                (recent_log, datetime(2026, 4, 25)),
            ],
            keep_days=30,
            now=now,
        )

        assert deleted == 1
        assert not old_log.exists()
        assert recent_log.exists()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_prune_logs_keeps_logs_a_process_still_writes(tmp_path):
    # After a crash, startup marks a run as failed while its rclone may live on, writing.
    now = datetime(2026, 4, 26, tzinfo=UTC)
    log = tmp_path / "survivor.log"
    with log.open("wb") as output:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], stdout=output
        )
    try:
        records = [(log, now - timedelta(days=31))]
        assert prune_logs(records, keep_days=30, now=now, in_use=open_files()) == 0
        assert log.exists()
    finally:
        child.kill()
        child.wait()
    assert prune_logs(records, keep_days=30, now=now, in_use=open_files()) == 1
    assert not log.exists()


def test_open_files_reads_proc_like_listings(tmp_path):
    proc = tmp_path / "proc"
    (proc / "12" / "fd").mkdir(parents=True)
    (proc / "12" / "fd" / "1").symlink_to("/data/logs/job-1/run.log")
    (proc / "12" / "fd" / "2").symlink_to("pipe:[1234]")
    (proc / "self").mkdir()
    (proc / "34").mkdir()  # No fd listing: gone, or another user's.

    assert open_files(proc) == {"/data/logs/job-1/run.log"}
    assert open_files(tmp_path / "missing") == set()

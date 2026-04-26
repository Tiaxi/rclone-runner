import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.retention import prune_logs


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

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path


def prune_logs(
    log_records: Iterable[tuple[Path, datetime]],
    keep_days: int,
    now: datetime | None = None,
) -> int:
    if keep_days <= 0:
        return 0
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(days=keep_days)
    deleted = 0
    for path, created_at in log_records:
        if created_at >= cutoff or not path.exists():
            continue
        path.unlink()
        deleted += 1
    return deleted

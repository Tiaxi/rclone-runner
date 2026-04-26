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
    current_time = _as_utc(now or datetime.now(UTC))
    cutoff = current_time - timedelta(days=keep_days)
    deleted = 0
    for path, created_at in log_records:
        if _as_utc(created_at) >= cutoff or not path.exists():
            continue
        path.unlink()
        deleted += 1
    return deleted


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

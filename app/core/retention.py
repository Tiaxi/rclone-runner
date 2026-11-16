from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROC = Path("/proc")


def prune_logs(
    log_records: Iterable[tuple[Path, datetime]],
    keep_days: int,
    now: datetime | None = None,
    in_use: set[str] | None = None,
) -> int:
    """Delete logs started more than keep_days ago, except those in in_use (real paths of
    files some process still has open: deleting one would lose the rest of its output)."""
    if keep_days <= 0:
        return 0
    current_time = _as_utc(now or datetime.now(UTC))
    cutoff = current_time - timedelta(days=keep_days)
    deleted = 0
    for path, created_at in log_records:
        if _as_utc(created_at) >= cutoff or not path.exists():
            continue
        if in_use and os.path.realpath(path) in in_use:
            continue
        path.unlink()
        deleted += 1
    return deleted


def open_files(proc: Path = PROC) -> set[str]:
    """The files the processes this one can see have open, from /proc (Linux).

    A step's rclone can outlive the app: after a crash, startup marks its run as failed while
    the process may still be writing the log. Without /proc, the set is empty.
    """
    paths: set[str] = set()
    try:
        fd_dirs = [entry / "fd" for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return paths
    for fd_dir in fd_dirs:
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue  # Gone, or another user's.
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("/"):
                paths.add(target)
    return paths


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

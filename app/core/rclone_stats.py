from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

# rclone prints its final stats block last, so the end of the log is enough. Reading only the
# tail keeps page views and notifications cheap for multi-gigabyte -vv logs.
STATS_TAIL_BYTES = 1024 * 1024
# Stored for a finished step whose log had no stats, so the log is not parsed again.
STATS_UNAVAILABLE_JSON = "null"


@dataclass(frozen=True, slots=True)
class RcloneTransferStats:
    transferred_bytes: int
    transferred_files: int
    deleted_files: int = 0


@dataclass(frozen=True, slots=True)
class StepStatsDisplay:
    stats: RcloneTransferStats | None
    label: str
    available: bool
    transferred_data_label: str
    transferred_files_label: str
    deleted_files_label: str


@dataclass(frozen=True, slots=True)
class RunStatsDisplay:
    transferred_bytes: int
    transferred_files: int
    deleted_files: int
    transferred_data_label: str
    transferred_files_label: str
    deleted_files_label: str
    has_unavailable: bool


_DATA_TRANSFER_RE = re.compile(r"^\s*Transferred:\s+([\d.]+)\s*([A-Za-z]+)\s*/", re.MULTILINE)
_FILE_TRANSFER_RE = re.compile(r"^\s*Transferred:\s+([0-9,]+)\s*/\s*([0-9,]+)", re.MULTILINE)
_DELETED_RE = re.compile(r"^\s*Deleted:\s+([0-9,]+)\s+\(files\)", re.MULTILINE)

_BYTE_MULTIPLIERS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "PiB": 1024**5,
}


def parse_rclone_transfer_stats(text: str) -> RcloneTransferStats | None:
    data_matches = list(_DATA_TRANSFER_RE.finditer(text))
    file_matches = list(_FILE_TRANSFER_RE.finditer(text))
    if not data_matches:
        return None

    data_match = data_matches[-1]
    transferred_bytes = _parse_byte_count(data_match.group(1), data_match.group(2))
    if transferred_bytes is None:
        return None
    transferred_files = 0
    if file_matches:
        parsed_files = _parse_count(file_matches[-1].group(1))
        if parsed_files is None:
            return None
        transferred_files = parsed_files
    elif transferred_bytes != 0:
        return None

    deleted_matches = list(_DELETED_RE.finditer(text))
    deleted_files = _parse_count(deleted_matches[-1].group(1)) if deleted_matches else 0
    if deleted_files is None:
        return None

    return RcloneTransferStats(
        transferred_bytes=transferred_bytes,
        transferred_files=transferred_files,
        deleted_files=deleted_files,
    )


def stats_to_json(value: RcloneTransferStats) -> str:
    return json.dumps(asdict(value), sort_keys=True)


def stats_from_json(value: str | None) -> RcloneTransferStats | None:
    if not value:
        return None
    try:
        raw = json.loads(value)
        return RcloneTransferStats(
            transferred_bytes=int(raw["transferred_bytes"]),
            transferred_files=int(raw["transferred_files"]),
            deleted_files=int(raw.get("deleted_files", 0)),
        )
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        return None


def stats_from_log(path: Path) -> RcloneTransferStats | None:
    text = _read_log_tail(path, STATS_TAIL_BYTES)
    return None if text is None else parse_rclone_transfer_stats(text)


@dataclass(frozen=True, slots=True)
class LiveProgress:
    """The latest progress rclone reported while a step is still running."""

    transferred: str
    total: str
    percent: str | None
    speed: str
    eta: str | None

    @property
    def label(self) -> str:
        parts = [f"{self.transferred} / {self.total}"]
        if self.percent:
            parts[0] += f" ({self.percent})"
        parts.append(self.speed)
        if self.eta:
            parts.append(f"ETA {self.eta}")
        return " · ".join(parts)


# rclone prints progress every minute at INFO level, so only with --verbose (-v). It matches
# both the multi-line block ("Transferred: 1.2 GiB / 2 TiB, 5%, 50 MiB/s, ETA 9h") and
# --stats-one-line output.
_SIZE = r"\d+(?:\.\d+)?\s*[KMGTPE]?i?B"
_PROGRESS_RE = re.compile(
    rf"(?P<transferred>{_SIZE})\s*/\s*(?P<total>{_SIZE}),\s*(?P<percent>\d+%|-),\s*"
    rf"(?P<speed>{_SIZE}/s),\s*ETA\s*(?P<eta>[^\s,]+)"
)
PROGRESS_TAIL_BYTES = 64 * 1024


def live_progress_from_log(path: Path) -> LiveProgress | None:
    text = _read_log_tail(path, PROGRESS_TAIL_BYTES)
    if text is None:
        return None
    matches = list(_PROGRESS_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    return LiveProgress(
        transferred=match["transferred"],
        total=match["total"],
        percent=None if match["percent"] == "-" else match["percent"],
        speed=match["speed"],
        eta=None if match["eta"] == "-" else match["eta"],
    )


def _read_log_tail(path: Path, max_bytes: int) -> str | None:
    """The last max_bytes of a log, starting at a line boundary."""
    try:
        with path.open("rb") as log_file:
            size = log_file.seek(0, 2)
            start = max(0, size - max_bytes)
            # Read from one byte earlier, so a tail that starts exactly at a line boundary
            # keeps its first line: only text up to the first newline is dropped.
            log_file.seek(max(0, start - 1))
            data = log_file.read()
    except OSError:
        return None
    if start > 0:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace")


class _StepRunStats(Protocol):
    transfer_stats_json: str | None
    ended_at: object
    log_path: str


def step_transfer_stats(step_run: _StepRunStats) -> RcloneTransferStats | None:
    """Stats for a step run: stored when it finished, or parsed from older runs' logs."""
    if step_run.transfer_stats_json is not None:
        return stats_from_json(step_run.transfer_stats_json)
    if step_run.ended_at is None:
        return None
    return stats_from_log(Path(step_run.log_path))


def step_stats_display(
    stats: RcloneTransferStats | None, status: str | None = None
) -> StepStatsDisplay:
    if stats is None:
        # rclone prints no transfer line when there was nothing to do, so missing stats on a
        # successful step mean no changes. A failed or canceled step may simply have stopped
        # before printing them.
        label = "Not available" if status in {"failed", "canceled"} else "No changes"
        return StepStatsDisplay(
            stats=None,
            label=label,
            available=False,
            transferred_data_label=label,
            transferred_files_label=label,
            deleted_files_label=label,
        )
    return StepStatsDisplay(
        stats=stats,
        label=(
            f"{format_byte_count(stats.transferred_bytes)}, "
            f"{stats.transferred_files} files, {stats.deleted_files} deleted"
        ),
        available=True,
        transferred_data_label=format_byte_count(stats.transferred_bytes),
        transferred_files_label=str(stats.transferred_files),
        deleted_files_label=str(stats.deleted_files),
    )


def pending_stats_display(label: str = "Pending") -> StepStatsDisplay:
    return StepStatsDisplay(
        stats=None,
        label=label,
        available=False,
        transferred_data_label=label,
        transferred_files_label=label,
        deleted_files_label=label,
    )


def run_stats_display(
    values: list[RcloneTransferStats | None], *, unavailable_count: int = 0
) -> RunStatsDisplay:
    available = [value for value in values if value is not None]
    transferred_bytes = sum(value.transferred_bytes for value in available)
    transferred_files = sum(value.transferred_files for value in available)
    deleted_files = sum(value.deleted_files for value in available)
    return RunStatsDisplay(
        transferred_bytes=transferred_bytes,
        transferred_files=transferred_files,
        deleted_files=deleted_files,
        transferred_data_label=format_byte_count(transferred_bytes),
        transferred_files_label=str(transferred_files),
        deleted_files_label=str(deleted_files),
        has_unavailable=False,
    )


def format_byte_count(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    amount = float(value)
    unit = "B"
    for unit in units:
        amount /= 1024
        if amount < 1024 or unit == units[-1]:
            break
    if unit == "KiB" and amount.is_integer():
        return f"{int(amount)} KiB"
    return f"{amount:.3f} {unit}"


def _parse_byte_count(amount: str, unit: str) -> int | None:
    multiplier = _BYTE_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    try:
        return int(float(amount.replace(",", "")) * multiplier)
    except ValueError:
        return None


def _parse_count(value: str) -> int | None:
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None

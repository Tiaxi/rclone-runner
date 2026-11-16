from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

# rclone prints its final stats block last, so the end of the log is enough. Reading only the
# tail keeps page views and notifications cheap for multi-gigabyte -vv logs.
STATS_TAIL_BYTES = 1024 * 1024
# Stored for a finished step whose log had no stats, so the log is not parsed again.
STATS_UNAVAILABLE_JSON = "null"
NOT_AVAILABLE = "Not available"


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
    def amount_label(self) -> str:
        amount = f"{self.transferred} / {self.total}"
        return f"{amount} ({self.percent})" if self.percent else amount

    @property
    def label(self) -> str:
        parts = [self.amount_label]
        parts.append(self.speed)
        if self.eta:
            parts.append(f"ETA {self.eta}")
        return " · ".join(parts)


# rclone prints progress every minute at INFO level, so only with --verbose (-v). It matches
# both the multi-line block ("Transferred: 1.2 GiB / 2 TiB, 5%, 50 MiB/s, ETA 9h") and
# --stats-one-line output, in bytes or, with --stats-unit bits, bits ("16.2 Mibit/s").
_SIZE = r"\d+(?:\.\d+)?\s*(?:[KMGTPE]i?)?(?:B|bit)"
_PROGRESS_RE = re.compile(
    rf"(?P<transferred>{_SIZE})\s*/\s*(?P<total>{_SIZE}),\s*(?P<percent>\d+%|-),\s*"
    rf"(?P<speed>{_SIZE}/s),\s*ETA\s*(?P<eta>[^\s,]+)"
)
# How much of a log to scan the first time a step is looked at; after that only new output is
# read, so a report is never lost however much is logged between reports.
PROGRESS_INITIAL_SCAN_BYTES = 1024 * 1024


class _ProgressTracker:
    """Follows running step logs and remembers the latest progress report in each.

    Status pages poll every second; reading only what was appended since the last poll keeps
    that cheap however large the log gets.
    """

    def __init__(self, max_logs: int = 64) -> None:
        self._max_logs = max_logs
        # log path -> (offset scanned up to, at a line start; latest report)
        self._logs: OrderedDict[str, tuple[int, LiveProgress | None]] = OrderedDict()

    def latest(self, path: Path) -> LiveProgress | None:
        key = str(path)
        offset, progress = self._logs.pop(key, (0, None))
        try:
            with path.open("rb") as log_file:
                size = log_file.seek(0, 2)
                if size < offset:  # rewritten from the start
                    offset, progress = 0, None
                start = max(offset, size - PROGRESS_INITIAL_SCAN_BYTES)
                log_file.seek(start)
                data = log_file.read(size - start)
        except OSError:
            return None
        skipped = 0
        if start > offset:
            # Started inside a line; skip to the next line start.
            newline = data.find(b"\n")
            skipped = newline + 1 if newline != -1 else len(data)
        complete_end = data.rfind(b"\n") + 1
        if complete_end > skipped:
            text = data[skipped:complete_end].decode("utf-8", errors="replace")
            matches = list(_PROGRESS_RE.finditer(text))
            if matches:
                progress = _progress_from_match(matches[-1])
            offset = start + complete_end
        else:
            offset = start + skipped
        self._logs[key] = (offset, progress)
        while len(self._logs) > self._max_logs:
            self._logs.popitem(last=False)
        return progress


def _progress_from_match(match: re.Match[str]) -> LiveProgress:
    return LiveProgress(
        transferred=match["transferred"],
        total=match["total"],
        percent=None if match["percent"] == "-" else match["percent"],
        speed=match["speed"],
        eta=None if match["eta"] == "-" else match["eta"],
    )


_progress_tracker = _ProgressTracker()


def live_progress_from_log(path: Path) -> LiveProgress | None:
    return _progress_tracker.latest(path)


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
        # rclone prints its totals only when its log level shows them (with -v, or
        # --stats-log-level NOTICE), and a failed or canceled step may have stopped first: a
        # log without them says nothing about what changed.
        return _unavailable_display()
    if status not in {"failed", "canceled"} and not (
        stats.transferred_bytes or stats.transferred_files or stats.deleted_files
    ):
        # Totals of zero, as rclone printed them: confirmed to have changed nothing. A failed
        # or canceled step keeps its numbers.
        label = "No changes"
        return StepStatsDisplay(
            stats=stats,
            label=label,
            available=True,
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


def _unavailable_display() -> StepStatsDisplay:
    label = NOT_AVAILABLE
    return StepStatsDisplay(
        stats=None,
        label=label,
        available=False,
        transferred_data_label=label,
        transferred_files_label=label,
        deleted_files_label=label,
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


def run_stats_display(values: list[RcloneTransferStats | None]) -> RunStatsDisplay:
    """Totals of a run's finished steps. None stands for a step whose log had no totals.

    With some steps unknown the totals are only a lower bound, and say so; with all of them
    unknown there is nothing to add up.
    """
    available = [value for value in values if value is not None]
    has_unavailable = len(available) < len(values)
    transferred_bytes = sum(value.transferred_bytes for value in available)
    transferred_files = sum(value.transferred_files for value in available)
    deleted_files = sum(value.deleted_files for value in available)
    labels = [format_byte_count(transferred_bytes), str(transferred_files), str(deleted_files)]
    if has_unavailable:
        labels = [f"at least {label}" for label in labels] if available else [NOT_AVAILABLE] * 3
    return RunStatsDisplay(
        transferred_bytes=transferred_bytes,
        transferred_files=transferred_files,
        deleted_files=deleted_files,
        transferred_data_label=labels[0],
        transferred_files_label=labels[1],
        deleted_files_label=labels[2],
        has_unavailable=has_unavailable,
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

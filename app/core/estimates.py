"""Estimate how long a step takes to upload, from its size and a transfer speed.

The speed comes, in order of preference, from the job's own recent runs, from the expected
upload speed set in Settings, or from the job's --bwlimit; a --bwlimit also caps the others.
Only runs that moved enough data for long enough count, because short runs spend most of their
time listing and checking rather than uploading.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import median

from sqlalchemy.orm import Session

from app.db import SettingRecord

EXPECTED_SPEED_KEY = "expected_upload_speed"
MIB = 1024 * 1024
MIN_SAMPLE_BYTES = 256 * MIB
MIN_SAMPLE_SECONDS = 60
SAMPLE_RUNS = 10
_UNIT_BYTES = {"B": 1, "K": 1024, "M": MIB, "G": 1024 * MIB, "T": 1024**4, "P": 1024**5}


@dataclass(frozen=True)
class Speed:
    bytes_per_second: float
    explanation: str

    @property
    def label(self) -> str:
        return f"{self.bytes_per_second / MIB:.1f} MiB/s"


def load_expected_speed(db: Session) -> float | None:
    """The expected upload speed from Settings, in MiB/s, or None when unset."""
    record = db.get(SettingRecord, EXPECTED_SPEED_KEY)
    if record is None:
        return None
    try:
        value = float(json.loads(record.value))
    except TypeError, ValueError:
        return None
    return value if value > 0 and math.isfinite(value) else None


def save_expected_speed(db: Session, mib_per_second: float | None) -> None:
    record = db.get(SettingRecord, EXPECTED_SPEED_KEY)
    if mib_per_second is None:
        if record is not None:
            db.delete(record)
    elif record is None:
        db.add(SettingRecord(key=EXPECTED_SPEED_KEY, value=json.dumps(mib_per_second)))
    else:
        record.value = json.dumps(mib_per_second)
    db.commit()


def observed_speed(samples: list[tuple[int, float]]) -> tuple[float, int] | None:
    """Median bytes per second of (bytes, seconds) samples big enough to mean something,
    newest first, and how many counted."""
    rates = [
        transferred / seconds
        for transferred, seconds in samples
        if transferred >= MIN_SAMPLE_BYTES and seconds >= MIN_SAMPLE_SECONDS
    ][:SAMPLE_RUNS]
    if not rates:
        return None
    return median(rates), len(rates)


def bwlimit_bytes_per_second(rate: str | None) -> float | None:
    """The upload limit of a single --bwlimit value in bytes per second, or None if unlimited."""
    if rate is None:
        return None
    upload = rate.split(":", 1)[0]
    if upload.lower() == "off":
        return None
    unit = upload[-1].upper()
    if unit.isdigit():
        return float(upload) * _UNIT_BYTES["K"]  # rclone reads a bare number as KiB/s
    return float(upload[:-1]) * _UNIT_BYTES[unit]


def choose_speed(
    observed: tuple[float, int] | None,
    expected_mib_per_second: float | None,
    bwlimit: float | None,
) -> Speed | None:
    if observed is not None:
        rate, count = observed
        source = (
            "this job's last sizeable run"
            if count == 1
            else f"the median of this job's last {count} sizeable runs"
        )
        speed = Speed(rate, source)
    elif expected_mib_per_second is not None:
        speed = Speed(expected_mib_per_second * MIB, "the expected upload speed in Settings")
    elif bwlimit is not None:
        return Speed(bwlimit, "the job's --bwlimit")
    else:
        return None
    if bwlimit is not None and bwlimit < speed.bytes_per_second:
        return Speed(bwlimit, f"the job's --bwlimit, which is lower than {speed.explanation}")
    return speed


def estimate_seconds(size_bytes: int, speed: Speed) -> float:
    return size_bytes / speed.bytes_per_second


def format_estimate(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    # Beyond an hour, five-minute steps are as precise as an estimate gets.
    minutes = 5 * round(minutes / 5)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"about {hours} h {minutes} min" if minutes else f"about {hours} h"
    days, hours = divmod(hours, 24)
    unit = "day" if days == 1 else "days"
    return f"about {days} {unit} {hours} h" if hours else f"about {days} {unit}"

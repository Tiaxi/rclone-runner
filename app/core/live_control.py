"""Change the bandwidth limit of running rclone steps, and pause them by throttling.

Each controllable step starts rclone with its remote control API on a Unix socket, in a
directory only this process's user can open. The API runs without authentication
(``--rc-no-auth``, which rclone requires for ``core/bwlimit`` without a password). That gives
away nothing: the same user can already run any rclone command from the console.

Pausing throttles to a low rate instead of stopping rclone, so connections stay open and
progress keeps reporting. The rate is not lower than ``PAUSE_RATE_PER_TRANSFER`` per parallel
transfer, because rclone applies a new limit only after each transfer's current read (about
32 KiB) has passed the old one: at 1 KiB/s, resuming four transfers took over two minutes. The
chosen limit and the paused state carry over to later steps of the same run through
``--bwlimit`` on their command line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# KiB/s per parallel transfer while paused: resuming then takes about two seconds.
PAUSE_RATE_PER_TRANSFER = 16
RCLONE_DEFAULT_TRANSFERS = 4
CONNECT_ATTEMPTS = 10
CONNECT_RETRY_SECONDS = 0.2
CONNECT_TIMEOUT_SECONDS = 5.0
# rclone answers a limit change only after its transfers' current reads pass the old limit.
BWLIMIT_REPLY_TIMEOUT_SECONDS = 300.0

_ONE_RATE = r"(?:off|\d+(?:\.\d+)?[bkmgtp]?)"
# ASCII only: rclone rejects other digits, and a look-alike such as the Kelvin sign would
# match case-insensitively.
_RATE_PATTERN = re.compile(rf"^{_ONE_RATE}(?::{_ONE_RATE})?$", re.IGNORECASE | re.ASCII)
_UNIT_LABELS = {"B": "B/s", "K": "KiB/s", "M": "MiB/s", "G": "GiB/s", "T": "TiB/s", "P": "PiB/s"}
# Subcommands whose own flags use the remote control address, or that are servers themselves.
_UNCONTROLLED_SUBCOMMANDS = {"rc", "rcd"}


class RateError(ValueError):
    """The bandwidth limit is not one rclone's --bwlimit accepts as a single value."""


class RcError(RuntimeError):
    """rclone's remote control API refused the call or could not be reached."""


def normalize_rate(value: str) -> str:
    rate = value.strip().replace(" ", "")
    if not _RATE_PATTERN.match(rate):
        raise RateError(
            f"{value!r} is not a bandwidth limit. Use a number with K, M or G "
            "(for example 750K or 8M), or off."
        )
    return ":".join("off" if part.lower() == "off" else part.upper() for part in rate.split(":"))


def rate_label(rate: str | None) -> str:
    if rate is None:
        return "Not limited"
    if ":" in rate:
        upload, download = rate.split(":", 1)
        return f"{_single_rate_label(upload)} up, {_single_rate_label(download)} down"
    return _single_rate_label(rate)


def _single_rate_label(rate: str) -> str:
    if rate.lower() == "off":
        return "Unlimited"
    unit = rate[-1].upper()
    if unit.isdigit():
        # rclone reads a bare number as KiB/s.
        return f"{rate} KiB/s"
    return f"{rate[:-1]} {_UNIT_LABELS[unit]}"


def with_flags(argv: list[str], flags: list[str]) -> list[str]:
    """Add flags to a command right after its subcommand.

    Nothing there can be another flag's value or follow an option terminator, which the end
    of the command can (``--backup-dir --`` names a folder called "--").
    """
    return [*argv[:2], *flags, *argv[2:]]


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The last value a command gives a flag (rclone uses the last), or None."""
    value = None
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith(f"{flag}="):
            value = arg.split("=", 1)[1]
    return value


def configured_rate(argv: list[str]) -> str | None:
    """The single --bwlimit value in a command, or None when unset or a timetable."""
    value = _flag_value(argv, "--bwlimit")
    if value is None:
        return None
    try:
        return normalize_rate(value)
    except RateError:
        return None


# Python's weekday numbers, from Monday; rclone takes short or full English names.
_DAYS = {
    name: number
    for number, names in enumerate(
        [
            ("mon", "monday"),
            ("tue", "tuesday"),
            ("wed", "wednesday"),
            ("thu", "thursday"),
            ("fri", "friday"),
            ("sat", "saturday"),
            ("sun", "sunday"),
        ]
    )
    for name in names
}
_TIME = re.compile(r"^(?:(?P<day>[a-z]+)-)?(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})$", re.ASCII)
_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class JobBandwidth:
    """A step's own bandwidth limit, as rclone reads it: the last --bwlimit in its command,
    else RCLONE_BWLIMIT in its environment. Either may be a timetable, such as
    "08:00,512k 23:00,off" or "Mon-08:00,1M Sat-00:00,off", which rclone follows by itself
    but which its remote control can't set: only the rate in force now can be put back."""

    value: str
    source: str  # "arguments" or "environment"
    # (minute of the week from Monday 00:00, rate), sorted; empty for a single rate.
    timetable: tuple[tuple[int, str], ...] = field(default=())
    rate: str | None = None

    @property
    def is_timetable(self) -> bool:
        return bool(self.timetable)

    def rate_at(self, now: datetime) -> str:
        """The rate rclone uses at this local time."""
        if not self.timetable:
            return self.rate or "off"
        minute = now.weekday() * _MINUTES_PER_DAY + now.hour * 60 + now.minute
        current = self.timetable[-1][1]  # Before the week's first entry: the last one's.
        for start, rate in self.timetable:
            if start > minute:
                break
            current = rate
        return current


def job_bandwidth(argv: list[str], env: dict[str, str]) -> JobBandwidth | None:
    """The bandwidth limit a step starts with, from its command or environment, or None.

    env is the step's whole environment (the container's and the job's): rclone reads
    RCLONE_BWLIMIT from it when the command has no --bwlimit.
    """
    value = _flag_value(argv, "--bwlimit")
    source = "arguments"
    if value is None:
        value = env.get("RCLONE_BWLIMIT")
        source = "environment"
    if value is None or not value.strip():
        return None
    if "," not in value:
        try:
            rate = normalize_rate(value)
        except RateError:
            logger.warning("Could not read the bandwidth limit %r", value)
            return None
        return JobBandwidth(value=value, source=source, rate=rate)
    try:
        timetable = _parse_timetable(value)
    except RateError, ValueError:
        logger.warning("Could not read the bandwidth timetable %r", value)
        return None
    return JobBandwidth(value=value, source=source, timetable=timetable)


def _parse_timetable(value: str) -> tuple[tuple[int, str], ...]:
    """rclone's timetable: entries "HH:MM,rate" (every day) or "Day-HH:MM,rate"."""
    entries: dict[int, str] = {}
    for entry in value.split():
        when, separator, rate_text = entry.partition(",")
        match = _TIME.match(when.lower())
        if not separator or match is None:
            raise ValueError(f"not a timetable entry: {entry!r}")
        hour, minute = int(match["hour"]), int(match["minute"])
        if hour > 23 or minute > 59:
            raise ValueError(f"not a time: {when!r}")
        rate = normalize_rate(rate_text)
        day = match["day"]
        if day is None:
            days = range(7)
        elif day in _DAYS:
            days = [_DAYS[day]]
        else:
            raise ValueError(f"not a day: {day!r}")
        for weekday in days:
            entries[weekday * _MINUTES_PER_DAY + hour * 60 + minute] = rate
    if not entries:
        raise ValueError("empty timetable")
    return tuple(sorted(entries.items()))


def pause_rate(argv: list[str], env: dict[str, str] | None = None) -> str:
    """The throttle for pausing this command: a little per parallel transfer."""
    transfers = RCLONE_DEFAULT_TRANSFERS
    value = _flag_value(argv, "--transfers")
    if value is None:
        value = (env or {}).get("RCLONE_TRANSFERS")
    if value is not None and value.isascii() and value.isdigit():
        transfers = max(1, int(value))
    return f"{PAUSE_RATE_PER_TRANSFER * transfers}K"


def supports_live_control(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[1] in _UNCONTROLLED_SUBCOMMANDS:
        return False
    # The job sets up its own remote control; adding ours would mix the two.
    return not any(arg == "--rc" or arg.startswith("--rc-") for arg in argv)


@dataclass
class RunControl:
    """Live control state for one job run. Its steps run one at a time."""

    limit: str | None = None
    paused: bool = False
    socket_path: Path | None = None
    # The current step's own limit, put back when a pause or the run's limit ends.
    job_limit: JobBandwidth | None = None
    pause_rate: str = f"{PAUSE_RATE_PER_TRANSFER * RCLONE_DEFAULT_TRANSFERS}K"
    # The one task sending limit changes to one step's rclone, so they arrive in order, and
    # the socket it sends to: a new step needs a new task.
    apply_task: asyncio.Task[None] | None = None
    apply_socket: Path | None = None

    def spawn_args(self, socket_path: Path) -> list[str]:
        args = ["--rc", f"--rc-addr=unix://{socket_path}", "--rc-no-auth"]
        rate = self.target_rate(for_new_process=True)
        if rate is not None:
            # These go before the job's own arguments, so a --bwlimit there would win; the
            # runner also sends this rate through the socket once rclone is up.
            args.append(f"--bwlimit={rate}")
        return args

    @property
    def configured(self) -> str | None:
        """The rate the job's own setting gives now, or None without one."""
        if self.job_limit is None:
            return None
        return self.job_limit.rate_at(_now())

    def target_rate(self, for_new_process: bool = False) -> str | None:
        if self.paused:
            return self.pause_rate
        if self.limit is not None:
            return self.limit
        # A new process already starts with the job's own --bwlimit or RCLONE_BWLIMIT.
        return None if for_new_process else (self.configured or "off")

    def _job_limit_detail(self) -> str:
        if self.job_limit is None:
            return "The job sets no limit"
        where = "arguments" if self.job_limit.source == "arguments" else "RCLONE_BWLIMIT"
        if self.job_limit.is_timetable:
            return f"Now, by the timetable in the job's {where}"
        return f"From the job's {where}"

    def state(self) -> dict[str, object]:
        available = self.socket_path is not None
        # A pause only takes effect on a step rclone runs with our control socket; on any
        # other step it waits for the next one.
        paused_now = self.paused and available
        if paused_now:
            label = "Paused"
            detail = f"Throttled to {rate_label(self.pause_rate)}; connections stay open"
        elif self.paused:
            label = f"{rate_label(self.limit or self.configured)} (pause pending)"
            detail = "Takes effect when the next step starts"
        else:
            label = rate_label(self.limit or self.configured)
            detail = "Set for this run" if self.limit is not None else self._job_limit_detail()
        return {
            "available": available,
            "paused": self.paused,
            "paused_now": paused_now,
            "limit": self.limit,
            "limit_label": rate_label(self.limit) if self.limit is not None else None,
            "label": label,
            "detail": detail,
            "pause_label": rate_label(self.pause_rate),
            "source": "run" if self.limit is not None else "job",
        }


def _now() -> datetime:
    """The local time a timetable is read in: the app's time zone, which is the container's,
    as rclone's own clock is."""
    from zoneinfo import ZoneInfo

    from app.config import settings

    return datetime.now(ZoneInfo(settings.timezone))


async def set_rclone_bandwidth(socket_path: Path, rate: str) -> None:
    """Set the limit of the rclone listening on socket_path, waiting briefly for it to start."""
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            await rc_call(
                socket_path,
                "core/bwlimit",
                {"rate": rate},
                reply_timeout=BWLIMIT_REPLY_TIMEOUT_SECONDS,
            )
            return
        except FileNotFoundError, ConnectionRefusedError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(CONNECT_RETRY_SECONDS)


async def rc_call(
    socket_path: Path,
    method: str,
    params: dict[str, object],
    reply_timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> dict:
    """Call one rclone remote control method over its Unix socket."""
    body = json.dumps(params).encode()
    request = (
        f"POST /{method} HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)), CONNECT_TIMEOUT_SECONDS
    )
    try:
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), reply_timeout)
    finally:
        writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise RcError(f"unexpected reply from rclone: {status_line!r}") from exc
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in header_lines)
    }
    if headers.get("transfer-encoding", "").lower() == "chunked":
        payload = _dechunk(payload)
    try:
        data = json.loads(payload or b"{}")
    except json.JSONDecodeError as exc:
        raise RcError(f"unexpected reply from rclone (HTTP {status})") from exc
    if status != 200:
        raise RcError(str(data.get("error") or f"HTTP {status}"))
    return data


def _dechunk(payload: bytes) -> bytes:
    body = b""
    while payload:
        size_line, _, rest = payload.partition(b"\r\n")
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            break
        body += rest[:size]
        payload = rest[size + 2 :]
    return body

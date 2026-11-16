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
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
_UNIT_LABELS = {
    "B": "B/s",
    "K": "KiB/s",
    "M": "MiB/s",
    "G": "GiB/s",
    "T": "TiB/s",
    "P": "PiB/s",
    "E": "EiB/s",
}
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
    try:
        rate_bytes_per_second(rate)
    except RateError:
        return rate
    if rate[-1].isdigit() or rate[-1] == ".":
        # rclone reads a bare number as KiB/s.
        return f"{rate} KiB/s"
    if rate[-2:].lower() == "ib":
        number, unit = rate[:-3], rate[-3]
    elif rate[-1] in "iI":
        number, unit = rate[:-2], rate[-2]
    else:
        number, unit = rate[:-1], rate[-1]
    return f"{number} {_UNIT_LABELS[unit.upper()]}"


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


# rclone's day names (fs/bwtimetable.go), as Go numbers weekdays: from Sunday.
_DAYS = {
    name: number
    for number, names in enumerate(
        [
            ("sun", "sunday"),
            ("mon", "monday"),
            ("tue", "tuesday"),
            ("wed", "wednesday"),
            ("thu", "thursday"),
            ("fri", "friday"),
            ("sat", "saturday"),
        ]
    )
    for name in names
}
_HHMM = re.compile(r"^([0-9]{2}):([0-9]{2})$", re.ASCII)
# rclone's size suffixes (fs/sizesuffix.go): binary multiples; a bare number is KiB.
_MULTIPLIERS = {letter: 1024**power for power, letter in enumerate("kmgtpe", start=1)}
_DECIMAL = re.compile(
    r"^(?:[0-9](?:_?[0-9])*(?:\.(?:[0-9](?:_?[0-9])*)?)?|\.[0-9](?:_?[0-9])*)"
    r"(?:[eE][+-]?[0-9](?:_?[0-9])*)?$",
    re.ASCII,
)
_HEX = re.compile(
    r"^0[xX](?:_?[0-9a-fA-F])*(?:\.(?:[0-9a-fA-F](?:_?[0-9a-fA-F])*)?)?[pP][+-]?[0-9](?:_?[0-9])*$",
    re.ASCII,
)


def _go_float(text: str) -> float:
    """A number as Go's strconv.ParseFloat reads it, which rclone's sizes use: decimal or
    hexadecimal (0x1p3), an optional sign, underscores between digits, inf and nan."""
    body = text[1:] if text[:1] in "+-" else text
    if body.lower() in {"inf", "infinity", "nan"}:
        value = float(body)
    elif _DECIMAL.match(body):
        value = float(body.replace("_", ""))
    elif _HEX.match(body) and any(char not in "_." for char in body[2 : body.lower().index("p")]):
        value = float.fromhex(body.replace("_", ""))
    else:
        raise RateError(f"{text!r} is not a number")
    return -value if text.startswith("-") else value


def rate_bytes_per_second(value: str) -> float | None:
    """One side of a --bwlimit rate in bytes per second as rclone reads it; None for off.

    rclone takes 750, 750k, 750K, 750Ki, 750KiB (all KiB/s), 8M, 8MiB, 1.5G, 512B (bytes),
    and off; not 8MB. Raises RateError for anything else.
    """
    if value.lower() == "off":
        return None
    last = value[-1:]
    if last.isdigit() or last == ".":
        number, multiplier = value, 1024
    elif last in "bB":
        if len(value) > 2 and value[-2] == "i":
            number, multiplier = value[:-3], _MULTIPLIERS.get(value[-3].lower())
        else:
            number, multiplier = value[:-1], 1
    elif last in "iI" and len(value) > 1:
        number, multiplier = value[:-2], _MULTIPLIERS.get(value[-2].lower())
    else:
        number, multiplier = value[:-1], _MULTIPLIERS.get(last.lower())
    if multiplier is None:
        raise RateError(f"{value!r} is not a bandwidth limit rclone takes")
    amount = _go_float(number)
    if amount < 0:
        raise RateError(f"{value!r} is negative")
    return amount * multiplier


def _check_rate_pair(value: str) -> None:
    """A rate, or upload:download rates, as rclone takes it in --bwlimit."""
    for side in value.split(":", 1):
        rate_bytes_per_second(side)


@dataclass(frozen=True)
class JobBandwidth:
    """A step's own bandwidth limit: a single rate, or a timetable such as
    "Sun-08:00,512Ki Mon-08:00,512Ki ..." that rclone follows by itself.

    For a running step it is rclone's own reading of its --bwlimit or RCLONE_BWLIMIT, so it
    is what rclone does, flag values, units and all. Its remote control can't take a
    timetable: the runner sets the rate in force at each moment instead (see runner.py).
    """

    value: str
    # rclone's time slots, in the order given: (weekday from Sunday, HHMM, rate). A single
    # rate has none.
    slots: tuple[tuple[int, int, str], ...] = field(default=())
    # rclone's local time zone; None for the system's.
    timezone: tzinfo | None = None

    @property
    def is_timetable(self) -> bool:
        return bool(self.slots)

    def rate_at(self, when: datetime) -> str:
        """The rate rclone uses at this time, picked as rclone's LimitAt does."""
        if not self.slots:
            return self.value
        local = when.astimezone(self.timezone)
        now = ((local.weekday() + 1) % 7) * 10000 + local.hour * 100 + local.minute
        # Before the week's first slot, the week's last one is in force; on a tie, the later.
        current = self.slots[0]
        for slot in self.slots[1:]:
            if slot[0] * 10000 + slot[1] >= current[0] * 10000 + current[1]:
                current = slot
        closest = None
        for slot in self.slots:
            start = slot[0] * 10000 + slot[1]
            if now < start:
                continue
            difference = _minutes(now) - _minutes(start)
            if closest is None or difference <= closest:
                closest = difference
                current = slot
        return current[2]

    def rate_now(self) -> str:
        return self.rate_at(_now())


def _minutes(day_hhmm: int) -> int:
    return (day_hhmm // 10000) * 24 * 60 + (day_hhmm // 100 % 100) * 60 + day_hhmm % 100


def parse_bandwidth(value: str | None, env: dict[str, str]) -> JobBandwidth | None:
    """A --bwlimit value as rclone reads it, or None when unset, off or unreadable.

    env is the step's whole environment, where rclone finds its local time zone (TZ).
    """
    if not value or value.lower() == "off":
        return None
    try:
        if " " not in value and "," not in value:
            _check_rate_pair(value)
            return JobBandwidth(value=value)
        slots = _parse_timetable(value)
    except RateError, ValueError:
        logger.warning("Could not read the bandwidth limit %r", value)
        return None
    return JobBandwidth(value=value, slots=slots, timezone=_rclone_timezone(env))


def job_bandwidth(argv: list[str], env: dict[str, str]) -> JobBandwidth | None:
    """The bandwidth limit a step's command and environment set, for estimates before it
    runs: its last --bwlimit, else RCLONE_BWLIMIT.

    With rclone's flag list loaded, a "--bwlimit=2M" that is another flag's value (as in
    "--exclude --bwlimit=2M") or follows "--" doesn't count, as in rclone.
    """
    from app.core import sources

    value = None
    if sources.flags_available() and len(argv) > 1:
        flags, _paths = sources.split_command(argv)
        for flag in flags:
            name, has_value, inline = flag[0].partition("=")
            if name == "--bwlimit":
                value = inline if has_value else (flag[1] if len(flag) > 1 else None)
    else:
        value = _flag_value(argv, "--bwlimit")
    if value is None:
        value = env.get("RCLONE_BWLIMIT")
    return parse_bandwidth(value, env)


def may_set_bwlimit(argv: list[str], env: dict[str, str]) -> bool:
    """Whether a step might have a bandwidth limit of its own: any argument mentioning
    --bwlimit, wherever it is, or RCLONE_BWLIMIT in its environment."""
    return "RCLONE_BWLIMIT" in env or any(arg.partition("=")[0] == "--bwlimit" for arg in argv[2:])


def _parse_timetable(value: str) -> tuple[tuple[int, int, str], ...]:
    """rclone's timetable: entries "HH:MM,rate" (every day) or "Day-HH:MM,rate", separated by
    spaces or semicolons."""
    slots: list[tuple[int, int, str]] = []
    for entry in re.split(r"[ ;]+", value.strip(" ;")):
        when, *rest = entry.split(",")
        if len(rest) != 1:
            raise ValueError(f"not a timetable entry: {entry!r}")
        rate = rest[0]
        _check_rate_pair(rate)
        day_text, separator, hhmm = when.partition("-")
        if not separator:
            day_text, hhmm = None, when
        elif "-" in hhmm:
            raise ValueError(f"not a time: {when!r}")
        match = _HHMM.match(hhmm)
        if match is None or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError(f"not a time: {hhmm!r}")
        time = int(match[1]) * 100 + int(match[2])
        if day_text is None:
            slots.extend((day, time, rate) for day in range(7))
        elif day_text.lower() in _DAYS:
            slots.append((_DAYS[day_text.lower()], time, rate))
        else:
            raise ValueError(f"not a day: {day_text!r}")
    return tuple(slots)


def _rclone_timezone(env: dict[str, str]) -> tzinfo | None:
    """The local time zone rclone, a Go program, takes from TZ: unset is the system's, empty
    or unknown is UTC, ":Name" is Name, and "/path" a zone file."""
    value = env.get("TZ")
    if value is None:
        return None
    name = value.removeprefix(":")
    if not name or name == "UTC":
        return UTC
    try:
        if name.startswith("/"):
            with open(name, "rb") as zone_file:
                return ZoneInfo.from_file(zone_file)
        return ZoneInfo(name)
    except OSError, ValueError, ZoneInfoNotFoundError:
        return UTC


def pause_rate(argv: list[str], env: dict[str, str] | None = None) -> str:
    """The throttle for pausing this command: a little per parallel transfer. A first guess:
    the runner corrects it from rclone's own reading once rclone is up."""
    transfers = RCLONE_DEFAULT_TRANSFERS
    value = _flag_value(argv, "--transfers")
    if value is None:
        value = (env or {}).get("RCLONE_TRANSFERS")
    if value is not None and value.isascii() and value.isdigit():
        transfers = int(value)
    return pause_rate_for(transfers)


def pause_rate_for(transfers: int) -> str:
    return f"{PAUSE_RATE_PER_TRANSFER * max(1, transfers)}K"


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
    # The current step's own limit, put back when a pause or the run's limit ends, once
    # known: rclone tells it when it is up (see runner.py), unless the step has none.
    job_limit: JobBandwidth | None = None
    job_limit_known: bool = False
    pause_rate: str = f"{PAUSE_RATE_PER_TRANSFER * RCLONE_DEFAULT_TRANSFERS}K"
    # The one task sending limit changes to one step's rclone, so they arrive in order, and
    # the socket it sends to: a new step needs a new task.
    apply_task: asyncio.Task[None] | None = None
    apply_socket: Path | None = None

    def spawn_args(self, socket_path: Path) -> list[str]:
        args = ["--rc", f"--rc-addr=unix://{socket_path}", "--rc-no-auth"]
        rate = self.target_rate(for_new_process=True)
        # Only for a step known to have no limit of its own: a --bwlimit here would replace
        # one from RCLONE_BWLIMIT, and hide the step's own from rclone's answer about it.
        # The runner also sends the rate through the socket once rclone is up.
        if rate is not None and self.job_limit_known and self.job_limit is None:
            args.append(f"--bwlimit={rate}")
        return args

    @property
    def configured(self) -> str | None:
        """The rate the job's own setting gives now, or None without one or not known yet."""
        if self.job_limit is None:
            return None
        return self.job_limit.rate_now()

    def target_rate(self, for_new_process: bool = False) -> str | None:
        """The rate the step's rclone should have; None when nothing needs setting: a new
        process starts with the job's own limit, and until rclone has told it, it isn't
        known what to put back."""
        if self.paused:
            return self.pause_rate
        if self.limit is not None:
            return self.limit
        if for_new_process or not self.job_limit_known:
            return None
        return self.configured or "off"

    def _job_limit_detail(self) -> str:
        if not self.job_limit_known:
            return "The job's own limit"
        if self.job_limit is None:
            return "The job sets no limit"
        if self.job_limit.is_timetable:
            return "Now, by the job's timetable"
        return "The job's own limit"

    def state(self) -> dict[str, object]:
        available = self.socket_path is not None
        # A pause only takes effect on a step rclone runs with our control socket; on any
        # other step it waits for the next one.
        paused_now = self.paused and available
        current = self.limit or self.configured
        # Until rclone has said what its own limit is, it isn't known.
        current_label = (
            rate_label(current) if current or self.job_limit_known else "As the job sets"
        )
        if paused_now:
            label = "Paused"
            detail = f"Throttled to {rate_label(self.pause_rate)}; connections stay open"
        elif self.paused:
            label = f"{current_label} (pause pending)"
            detail = "Takes effect when the next step starts"
        else:
            label = current_label
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
    return datetime.now(UTC)


async def read_rclone_options(socket_path: Path, stop: asyncio.Event | None = None) -> dict:
    """rclone's main options as it reads them from its flags and environment, waiting
    briefly for it to start, but not once stop is set: then rclone has gone."""
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            reply = await rc_call(socket_path, "options/get", {"blocks": "main"})
            return reply.get("main") or {}
        except FileNotFoundError, ConnectionRefusedError:
            if attempt == CONNECT_ATTEMPTS - 1 or (stop is not None and stop.is_set()):
                raise
            await asyncio.sleep(CONNECT_RETRY_SECONDS)
    return {}


async def set_rclone_bwlimit_option(socket_path: Path, rate: str) -> None:
    """Replace rclone's --bwlimit option with a single rate, so its own timetable no longer
    changes the limit each time it moves to another slot."""
    await rc_call(socket_path, "options/set", {"main": {"BwLimit": rate}})


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

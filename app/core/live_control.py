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
from dataclasses import dataclass
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
_RATE_PATTERN = re.compile(rf"^{_ONE_RATE}(?::{_ONE_RATE})?$", re.IGNORECASE)
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


def configured_rate(argv: list[str]) -> str | None:
    """The single --bwlimit value in a command, or None when unset or a timetable."""
    value = None
    for index, arg in enumerate(argv):
        if arg == "--bwlimit" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--bwlimit="):
            value = arg.split("=", 1)[1]
    if value is None:
        return None
    try:
        return normalize_rate(value)
    except RateError:
        return None


def pause_rate(argv: list[str]) -> str:
    """The throttle for pausing this command: a little per parallel transfer."""
    transfers = RCLONE_DEFAULT_TRANSFERS
    for index, arg in enumerate(argv):
        value = None
        if arg == "--transfers" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--transfers="):
            value = arg.split("=", 1)[1]
        if value is not None and value.isdigit():
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
    configured: str | None = None
    pause_rate: str = f"{PAUSE_RATE_PER_TRANSFER * RCLONE_DEFAULT_TRANSFERS}K"
    # The one task sending limit changes to the current rclone, so they arrive in order.
    apply_task: asyncio.Task[None] | None = None

    def spawn_args(self, socket_path: Path) -> list[str]:
        args = ["--rc", "--rc-addr", f"unix://{socket_path}", "--rc-no-auth"]
        rate = self.target_rate(for_new_process=True)
        if rate is not None:
            # A later --bwlimit wins over one in the job's own arguments.
            args += ["--bwlimit", rate]
        return args

    def target_rate(self, for_new_process: bool = False) -> str | None:
        if self.paused:
            return self.pause_rate
        if self.limit is not None:
            return self.limit
        # A new process already starts with the job's own --bwlimit.
        return None if for_new_process else (self.configured or "off")

    def state(self) -> dict[str, object]:
        return {
            "available": self.socket_path is not None,
            "paused": self.paused,
            "limit": self.limit,
            "limit_label": rate_label(self.limit) if self.limit is not None else None,
            "label": "Paused" if self.paused else rate_label(self.limit or self.configured),
            "pause_label": rate_label(self.pause_rate),
            "source": "run" if self.limit is not None else "job",
        }


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

"""Measure the size of what a job step backs up, with ``rclone size``.

A step's source is the first path in its command. Finding it needs to know which flags take a
value (``--backup-dir X`` does, ``--fast-list`` does not), which rclone's own flag lists tell.

The measurement keeps the step's global flags, so filters, ``--config`` and backend options
apply as they do in the step, minus the ones that only matter for transfers or output (and would
needlessly change the measurement's identity, like a dated ``--backup-dir``) or that start
services. Flags of the step's own subcommand are dropped, since ``rclone size`` rejects them;
bisync's ``--filters-file`` becomes the equivalent ``--filter-from``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Subcommands whose first path is a source of files to back up.
SOURCE_SUBCOMMANDS = ("sync", "copy", "move", "copyto", "moveto", "bisync")
HELP_TIMEOUT_SECONDS = 10
FLAG_RETRY_SECONDS = 60

# Global flags that don't change which files rclone size counts.
_DROPPED_FLAGS = {
    "--backup-dir",
    "--suffix",
    "--suffix-keep-extension",
    "--compare-dest",
    "--copy-dest",
    "--bwlimit",
    "--bwlimit-file",
    "--max-delete",
    "--max-delete-size",
    "--max-transfer",
    "--cutoff-mode",
    "--transfers",
    "--dry-run",
    "-n",
    "--interactive",
    "-i",
    "--progress",
    "-P",
    "--verbose",
    "-v",
    "--quiet",
    "-q",
    # rclone size never transfers, so this would always make it fail.
    "--error-on-no-transfer",
    # Profiles go to files; a measurement would overwrite the step's own.
    "--cpuprofile",
    "--memprofile",
}
_DROPPED_PREFIXES = ("--rc", "--log-", "--stats", "--use-json-log", "--dump", "-vv")
# Local flags with a global equivalent.
_TRANSLATED_FLAGS = {("bisync", "--filters-file"): "--filter-from"}
# The type may be an enum such as HARD|SOFT|CAUTIOUS.
_FLAG_LINE = re.compile(r"^\s+(?:-(?P<short>\w), )?--(?P<long>[\w.-]+)(?: (?P<type>\S+))?\s{2,}")


@dataclass(frozen=True)
class FlagSet:
    names: frozenset[str]
    with_values: frozenset[str]


_flag_sets: dict[str, FlagSet] = {}


@dataclass(frozen=True)
class SizeCommand:
    source: str
    argv: list[str]
    # rclone reads remotes and options from RCLONE_* variables too, so two jobs with the same
    # command can still measure different things. A digest keeps secrets out of the key.
    env_digest: str = ""

    @property
    def key(self) -> str:
        """Identifies a measurement: the same command in the same rclone environment."""
        return json.dumps({"argv": self.argv, "env": self.env_digest})


@dataclass(frozen=True)
class SourceSize:
    bytes: int
    files: int


def _read_flags(command: list[str]) -> FlagSet | None:
    try:
        output = subprocess.run(
            command, capture_output=True, text=True, timeout=HELP_TIMEOUT_SECONDS, check=True
        ).stdout
    except OSError, subprocess.SubprocessError:
        logger.warning("Could not read rclone's flags from %s", " ".join(command))
        return None
    names: set[str] = set()
    with_values: set[str] = set()
    for line in output.splitlines():
        match = _FLAG_LINE.match(line)
        if not match:
            continue
        flag_names = [f"--{match['long']}"] + ([f"-{match['short']}"] if match["short"] else [])
        names.update(flag_names)
        # Typed flags take a value, except counters such as -v.
        if match["type"] and match["type"] != "count":
            with_values.update(flag_names)
    return FlagSet(frozenset(names), frozenset(with_values))


_EMPTY = FlagSet(frozenset(), frozenset())
_FLAG_LIST_NAMES = ("global", *SOURCE_SUBCOMMANDS)
_failed_at: dict[str, float] = {}


def _flags(name: str) -> FlagSet:
    """rclone's global flags ("global") or a subcommand's own, if loaded.

    Only reads what ensure_flags or load_flags loaded: request handling never starts rclone.
    """
    return _flag_sets.get(name, _EMPTY)


def flags_available() -> bool:
    return "global" in _flag_sets


def _due(name: str) -> bool:
    if name in _flag_sets:
        return False
    failed_at = _failed_at.get(name)
    return failed_at is None or time.monotonic() - failed_at >= FLAG_RETRY_SECONDS


def load_flags() -> None:
    """Read the flag lists not loaded yet. A failed read is retried after FLAG_RETRY_SECONDS,
    so a slow moment does not spoil detection until a restart, nor hold up every request."""
    for name in _FLAG_LIST_NAMES:
        if not _due(name):
            continue
        command = ["rclone", "help", "flags"] if name == "global" else ["rclone", name, "--help"]
        flags = _read_flags(command)
        if flags is None:
            _failed_at[name] = time.monotonic()
        else:
            _flag_sets[name] = flags
            _failed_at.pop(name, None)


async def ensure_flags() -> None:
    """Read rclone's flag lists in a thread, so request handlers never wait on rclone."""
    if any(_due(name) for name in _FLAG_LIST_NAMES):
        await asyncio.to_thread(load_flags)


def _flags_with_values(subcommand: str) -> frozenset[str]:
    return _flags("global").with_values | _flags(subcommand).with_values


def _global_flags() -> frozenset[str]:
    return _flags("global").names


def split_command(argv: list[str]) -> tuple[list[list[str]], list[str]]:
    """Split an rclone command after its subcommand into flags (with values) and paths.

    Short flags come out one by one, as rclone reads them (see _split_short): "-vf x" gives
    ["-v"] and ["-f", "x"].
    """
    subcommand = argv[1]
    takes_value = _flags_with_values(subcommand)
    flags: list[list[str]] = []
    paths: list[str] = []
    args = iter(argv[2:])
    for arg in args:
        if arg == "--":
            paths.extend(args)
            break
        if arg.startswith("--"):
            flag = [arg]
            if "=" not in arg and arg in takes_value:
                value = next(args, None)
                if value is not None:
                    flag.append(value)
            flags.append(flag)
        elif arg.startswith("-") and arg != "-":
            flags.extend(_split_short(arg, args, subcommand))
        else:
            paths.append(arg)
    return flags, paths


def _split_short(arg: str, args, subcommand: str) -> list[list[str]]:
    """A group of short flags, one flag each, read as rclone's flag parser (pflag) does.

    Each letter is a flag. "=value" after a letter is its value. A letter that takes a value
    takes the rest of the group ("-f- *.tmp", from -f'- *.tmp'), or the next argument when it
    ends the group ("-vf x"). Letters rclone doesn't list are kept together, unread: what
    follows them can't be told apart.
    """
    takes_value = _flags_with_values(subcommand)
    known = _flags("global").names | _flags(subcommand).names
    flags: list[list[str]] = []
    letters = arg[1:]
    while letters:
        name = f"-{letters[0]}"
        rest = letters[1:]
        if name not in known:
            flags.append([f"-{letters}"])
            break
        if rest.startswith("="):
            flags.append([f"{name}{rest}"])
            break
        if name in takes_value:
            if rest:
                flags.append([name, rest])
            else:
                value = next(args, None)
                flags.append([name] if value is None else [name, value])
            break
        flags.append([name])
        letters = rest
    return flags


def size_command(argv: list[str], env: dict[str, str] | None = None) -> SizeCommand | None:
    """The ``rclone size`` command for a step's source, or None if it has no source."""
    if len(argv) < 3 or argv[0] != "rclone" or argv[1] not in SOURCE_SUBCOMMANDS:
        return None  # Built-in steps such as prune-dated have no source either.
    if not flags_available():
        # Without rclone's flag list, a flag's value could pass for the source.
        return None
    flags, paths = split_command(argv)
    if not paths:
        return None
    global_flags = _global_flags()
    kept: list[str] = []
    for flag in flags:
        name, has_value, inline_value = flag[0].partition("=")
        translated = _TRANSLATED_FLAGS.get((argv[1], name))
        if translated is not None:
            flag = [f"{translated}={inline_value}"] if has_value else [translated, *flag[1:]]
            name = translated
        if _keeps(name, global_flags):
            kept.extend(flag)
    # "--" keeps a source that starts with "-" from being read as a flag.
    size_argv = ["rclone", "size", "--json", *kept, "--", paths[0]]
    return SizeCommand(source=paths[0], argv=size_argv, env_digest=_env_digest(env or {}))


def _keeps(name: str, global_flags: frozenset[str]) -> bool:
    if name in _DROPPED_FLAGS or name.startswith(_DROPPED_PREFIXES):
        return False
    return name in global_flags


def _env_digest(env: dict[str, str]) -> str:
    rclone_env = sorted((key, value) for key, value in env.items() if key.startswith("RCLONE_"))
    if not rclone_env:
        return ""
    return hashlib.sha256(json.dumps(rclone_env).encode()).hexdigest()[:16]


def parse_size_output(output: str) -> SourceSize:
    """Read ``rclone size --json`` output: {"count": files, "bytes": bytes, ...}."""
    data = json.loads(output)
    return SourceSize(bytes=int(data["bytes"]), files=int(data["count"]))

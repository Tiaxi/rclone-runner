"""Measure the size of what a job step backs up, with ``rclone size``.

A step's source is the first path in its command. Finding it needs to know which flags take a
value (``--backup-dir X`` does, ``--fast-list`` does not), which rclone's own flag list tells.
The measurement keeps the flags that decide which files count (filters, symlink handling, local
backend options), so the totals match what the step would look at, and drops everything else,
since ``rclone size`` rejects flags that belong to other subcommands.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Subcommands whose first path is a source of files to back up.
SOURCE_SUBCOMMANDS = {"sync", "copy", "move", "copyto", "moveto", "bisync"}

_FILTER_FLAGS = {
    "--exclude",
    "--exclude-from",
    "--exclude-if-present",
    "--include",
    "--include-from",
    "--filter",
    "--filter-from",
    "--files-from",
    "--files-from-raw",
    "--min-size",
    "--max-size",
    "--min-age",
    "--max-age",
    "--max-depth",
    "--ignore-case",
    "--hash-filter",
    "--copy-links",
    "--links",
    "--skip-links",
    "--one-file-system",
    "--fast-list",
}
_FILTER_SHORT_FLAGS = {"-L", "-l", "-x"}
_KEPT_PREFIXES = ("--metadata-include", "--metadata-exclude", "--metadata-filter", "--local-")
_FLAG_LINE = re.compile(r"^\s+(?:-(?P<short>\w), )?--(?P<long>[\w.-]+)(?: (?P<type>\w+))?\s{2,}")


@dataclass(frozen=True)
class SizeCommand:
    source: str
    argv: list[str]

    @property
    def key(self) -> str:
        """Identifies a measurement: the same source with the same filters."""
        return json.dumps(self.argv)


@dataclass(frozen=True)
class SourceSize:
    bytes: int
    files: int


@functools.cache
def _flags_with_values(subcommand: str) -> frozenset[str]:
    """Flag names (``--long`` and ``-s``) that take a value: rclone's global flags and the
    subcommand's own, from its help."""
    output = ""
    for command in (["rclone", "help", "flags"], ["rclone", subcommand, "--help"]):
        try:
            output += subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=True
            ).stdout
        except OSError, subprocess.SubprocessError:
            logger.warning("Could not read rclone's flags from %s", " ".join(command))
    names = set()
    for line in output.splitlines():
        match = _FLAG_LINE.match(line)
        # Typed flags take a value, except counters such as -v.
        if match and match["type"] and match["type"] != "count":
            names.add(f"--{match['long']}")
            if match["short"]:
                names.add(f"-{match['short']}")
    return frozenset(names)


def split_command(argv: list[str]) -> tuple[list[list[str]], list[str]]:
    """Split an rclone command after its subcommand into flags (with values) and paths."""
    takes_value = _flags_with_values(argv[1])
    flags: list[list[str]] = []
    paths: list[str] = []
    args = iter(argv[2:])
    for arg in args:
        if arg == "--":
            paths.extend(args)
            break
        if arg.startswith("-") and arg != "-":
            flag = [arg]
            if "=" not in arg and arg in takes_value:
                value = next(args, None)
                if value is not None:
                    flag.append(value)
            flags.append(flag)
        else:
            paths.append(arg)
    return flags, paths


def size_command(argv: list[str]) -> SizeCommand | None:
    """The ``rclone size`` command for a step's source, or None if it has no source."""
    if len(argv) < 3 or argv[1] not in SOURCE_SUBCOMMANDS:
        return None
    flags, paths = split_command(argv)
    if not paths:
        return None
    kept = [part for flag in flags if _keeps(flag[0]) for part in flag]
    return SizeCommand(source=paths[0], argv=["rclone", "size", "--json", *kept, paths[0]])


def _keeps(flag: str) -> bool:
    name = flag.split("=", 1)[0]
    return name in _FILTER_FLAGS or name in _FILTER_SHORT_FLAGS or name.startswith(_KEPT_PREFIXES)


def parse_size_output(output: str) -> SourceSize:
    """Read ``rclone size --json`` output: {"count": files, "bytes": bytes, ...}."""
    data = json.loads(output)
    return SourceSize(bytes=int(data["bytes"]), files=int(data["count"]))

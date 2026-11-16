"""The ``prune-dated`` step: purge dated backup folders by their names.

Jobs keep replaced and deleted files with ``--backup-dir remote:versions/${RUN_DATE}/...``, so
``remote:versions`` fills up with ``YYYY-MM-DD`` folders. ``rclone delete --min-age`` can't
clear them: moved files keep their original modification times, so they would be deleted the
day they arrive. This step goes by the folder names instead::

    prune-dated remote:versions --keep-days 30 --keep-min 7

purges each folder whose name is a date more than 30 days before the run's date (the
``${RUN_DATE}`` of the run, in the app's time zone), except that the 7 newest dated folders are
always kept, whatever their dates. Anything not named exactly ``YYYY-MM-DD`` is left alone.

Everything this step can't see or read is kept: a folder hidden by a filter, a listing line it
doesn't understand, or a date that doesn't exist. Other rclone flags in the step (given as
``--flag=value``) and the job's common arguments go to ``rclone lsf`` and ``rclone purge``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.core.aio import run_to_completion, stop_process

PRUNE_COMMAND = "prune-dated"
LOG_PREFIX = f"[{PRUNE_COMMAND}]"
TERMINATE_TIMEOUT_SECONDS = 5
# rclone's exit code for "directory not found".
RCLONE_DIRECTORY_NOT_FOUND = 3
# How many names of other entries the log lists before summing up the rest.
LOGGED_IGNORED_NAMES = 10

_DATED_NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COUNT = re.compile(r"[0-9]+")
_SHORT_FLAGS = re.compile(r"-[A-Za-z]+")
_REMOTE_NAME = re.compile(r"[^,:]*")
# In the order argv() stores them.
_OPTIONS = ("--keep-days", "--keep-min")
_FALSE_VALUES = {"false", "0", "f"}
# The values rclone's flag parser reads as true.
_TRUE_VALUES = {"1", "t", "T", "true", "TRUE", "True"}
# Flags left out of listing and purging, from the arguments and from rclone's environment
# (RCLONE_EXCLUDE). Filters are meant for the job's syncs, and purge refuses them ("filters are
# not supported with purge"); --files-from would hide every folder from the listing, and
# --max-depth would keep purge from reaching the files. --error-on-no-transfer would make both
# fail, since neither transfers. Following symlinks (-L) would let purge delete what a link in
# an old folder points to, such as a kept folder or something outside the path; the link
# flags all go, so none can undo the ones this step sets (see _PURGE_FLAGS).
_DROPPED_SWITCHES = {
    "--error-on-no-transfer",
    "--ignore-case",
    "--delete-excluded",
    "--copy-links",
    "--links",
    "--local-links",
}
_DROPPED_FLAGS_WITH_VALUES = {
    "--max-depth",
    "--exclude",
    "--exclude-from",
    "--exclude-if-present",
    "--files-from",
    "--files-from-raw",
    "--files-from0",
    "--filter",
    "--filter-from",
    "--hash-filter",
    "--include",
    "--include-from",
    "--max-age",
    "--max-size",
    "--metadata-exclude",
    "--metadata-exclude-from",
    "--metadata-filter",
    "--metadata-filter-from",
    "--metadata-include",
    "--metadata-include-from",
    "--min-age",
    "--min-size",
}
# -f is --filter, the only global short flag that takes a value; -L is --copy-links, -l is
# --links.
_SHORT_FILTER = "f"
_DROPPED_SHORT = {"L", "l"}
_DROPPED_ENV = {
    "RCLONE_" + name.removeprefix("--").upper().replace("-", "_")
    for name in _DROPPED_SWITCHES | _DROPPED_FLAGS_WITH_VALUES
} | {"RCLONE_LOCAL_COPY_LINKS"}
# Given first, so nothing in the job's flags can be read as their value. A flag on the command
# line beats the environment and the remote's config, so symlinks are never followed: a dated
# folder that is a link isn't listed, and purge deletes links inside a folder as links. Only a
# connection string beats them, which the path may not have; one in the remote's config that
# turns on copy_links makes rclone refuse --links ("can't use -l/--links with -L"), so purge
# fails rather than follow links.
_LIST_FLAGS = ["--copy-links=false", "--dirs-only", "--max-depth", "1", "--format", "p"]
_PURGE_FLAGS = ["--copy-links=false", "--links"]


class PruneCommandError(ValueError):
    """A prune-dated step that can't be run safely as written."""


@dataclass(frozen=True, slots=True)
class PruneCommand:
    path: str
    keep_days: int
    keep_min: int
    # rclone flags for lsf and purge: the job's common arguments, then the step's own flags.
    rclone_flags: tuple[str, ...] = ()

    def argv(self) -> list[str]:
        """The command as stored with a step run. ``from_argv`` reads it back."""
        return [
            PRUNE_COMMAND,
            self.path,
            "--keep-days",
            str(self.keep_days),
            "--keep-min",
            str(self.keep_min),
            *self.rclone_flags,
        ]

    @property
    def dry_run(self) -> bool:
        return dry_run_requested(self.rclone_flags)

    def target(self, name: str) -> str:
        return f"{self.path.rstrip('/')}/{name}"

    def description(self) -> str:
        """What a real run does, to follow "This step": "permanently deletes ..."."""
        return (
            f"permanently deletes dated folders in {self.path} older than "
            f"{_days(self.keep_days)}, always keeping the newest {self.keep_min}"
        )


@dataclass(frozen=True, slots=True)
class PrunePlan:
    purge: list[str]  # Oldest first.
    keep: list[str]  # Newest first.
    ignored: list[str]
    cutoff: date  # Folders dated before this are purged.


def is_prune_command(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == PRUNE_COMMAND


def parse_step(step_args: list[str], common_args: list[str]) -> PruneCommand:
    """Read a step's ``prune-dated`` arguments (after the command name)."""
    values: dict[str, int] = {}
    paths: list[str] = []
    flags: list[str] = []
    args = iter(step_args)
    for arg in args:
        name, has_value, value = arg.partition("=")
        if name in _OPTIONS:
            if name in values:
                raise PruneCommandError(f"{name} is given more than once")
            if not has_value:
                value = next(args, "")
            values[name] = _count(name, value)
        elif arg == "--":
            paths.extend(args)
        elif arg.startswith("-") and arg != "-":
            flags.append(arg)
        else:
            paths.append(arg)
    if len(paths) != 1:
        raise PruneCommandError(
            f"{PRUNE_COMMAND} takes one path, got {len(paths)}"
            + ("; give other flags' values with =, like --flag=value" if paths else "")
        )
    for name in _OPTIONS:
        if name not in values:
            raise PruneCommandError(f"{name} N is required")
    if values["--keep-min"] < 1:
        raise PruneCommandError("--keep-min must be at least 1")
    return PruneCommand(
        path=check_path(paths[0]),
        keep_days=values["--keep-days"],
        keep_min=values["--keep-min"],
        rclone_flags=(*common_args, *flags),
    )


def from_argv(argv: list[str]) -> PruneCommand:
    """Read back a stored command, checking it again before anything is deleted."""
    if len(argv) < 6 or argv[0] != PRUNE_COMMAND or (argv[2], argv[4]) != _OPTIONS:
        raise PruneCommandError("not a stored prune-dated command")
    command = PruneCommand(
        path=check_path(argv[1]),
        keep_days=_count("--keep-days", argv[3]),
        keep_min=_count("--keep-min", argv[5]),
        rclone_flags=tuple(argv[6:]),
    )
    if command.keep_min < 1:
        raise PruneCommandError("--keep-min must be at least 1")
    return command


def check_path(path: str) -> str:
    """Refuse an empty path, a remote's or the file system's root, and ``..``."""
    if not path.strip():
        raise PruneCommandError("the path is empty")
    remote, folder = _split_remote(path)
    if "," in remote or any(quote in remote for quote in "\"'"):
        # A connection string's options beat every flag, so copy_links=true there would make
        # purge follow symlinks (and quoted values may hide a colon). Set them in rclone's
        # config instead.
        raise PruneCommandError(
            "connection string options aren't supported in the path; set them in the remote's "
            "config"
        )
    parts = [part for part in folder.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PruneCommandError("the path must not contain ..")
    if not parts:
        raise PruneCommandError(f"refusing to prune the root of {remote or 'the file system'}")
    return path


def _split_remote(path: str) -> tuple[str, str]:
    """Split ``remote:folder`` like rclone: a colon before any slash ends a remote name.

    An on-the-fly remote (``:local:``) starts with a colon of its own, and options after a
    comma (``:sftp,key_file=/k:``) may hold slashes.
    """
    start = 1 if path.startswith(":") else 0
    name = _REMOTE_NAME.match(path, start)
    end = name.end()
    if end == len(path) or "/" in path[:end]:
        return "", path
    colon = path.find(":", end)
    if colon == -1:
        return "", path
    return path[: colon + 1], path[colon + 1 :]


def _count(name: str, value: str) -> int:
    if not _COUNT.fullmatch(value):
        raise PruneCommandError(f"{name} needs a whole number of days or folders, got {value!r}")
    return int(value)


def dry_run_requested(flags: Iterable[str]) -> bool:
    """Whether rclone would read these flags as a dry run.

    rclone uses the last value given (--dry-run=false), and -n may be combined with other
    short flags (-vn). Another flag's value that looks like one of these counts too, which
    only ever means not purging.
    """
    dry_run = False
    for flag in flags:
        name, has_value, value = flag.partition("=")
        if name == "--dry-run":
            dry_run = value.lower() not in _FALSE_VALUES if has_value else True
        elif _SHORT_FLAGS.fullmatch(name) and "n" in name:
            # In "-vn=false" the value belongs to the last letter.
            explicit = has_value and name.endswith("n")
            dry_run = value.lower() not in _FALSE_VALUES if explicit else True
    return dry_run


def rclone_flags(flags: Iterable[str]) -> list[str]:
    """The flags for rclone lsf and purge: all but the dropped ones (see _DROPPED_SWITCHES)."""
    kept: list[str] = []
    args = iter(flags)
    for arg in args:
        name, has_value, _value = arg.partition("=")
        if name in _DROPPED_SWITCHES:
            continue
        if name in _DROPPED_FLAGS_WITH_VALUES:
            if not has_value:
                next(args, None)
            continue
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            short = _short_flags(arg, args)
            if short is not None:
                kept.append(short)
            continue
        kept.append(arg)
    return kept


def _short_flags(arg: str, args) -> str | None:
    """A cluster of short flags without -f and its value, -L and -l, as rclone reads it.

    In "-vf x" the -f takes the next argument, in "-vf- *.tmp" (from -vf'- *.tmp') and
    "-f=x" the rest of this one; "=value" after another letter belongs to that letter.
    """
    kept = ""
    letters = arg[1:]
    for index, letter in enumerate(letters):
        if letter == _SHORT_FILTER:
            if index + 1 == len(letters):
                next(args, None)
            break
        if letter == "=":
            # The value of the letter before, kept with it if that letter was kept.
            if kept and letters[index - 1] not in _DROPPED_SHORT:
                kept += letters[index:]
            break
        if letter not in _DROPPED_SHORT:
            kept += letter
    return f"-{kept}" if kept else None


def rclone_environment(env: dict[str, str]) -> dict[str, str]:
    """rclone's environment for lsf and purge, without the dropped flags' variables."""
    return {key: value for key, value in env.items() if key not in _DROPPED_ENV}


def may_turn_off_dry_run(args: Iterable[str]) -> bool:
    """Whether any argument could set --dry-run (or -n) to false.

    Values rclone wouldn't read as true count too, and so does such an argument anywhere,
    even as another flag's value or a path: it only ever means refusing a dry run.
    """
    for arg in args:
        name, has_value, value = arg.partition("=")
        if not has_value or value in _TRUE_VALUES:
            continue
        # In "-vn=false" the value belongs to the last letter.
        if name == "--dry-run" or (_SHORT_FLAGS.fullmatch(name) and name.endswith("n")):
            return True
    return False


def dated_name(name: str) -> date | None:
    """The date a folder is named after, or None unless the name is exactly YYYY-MM-DD."""
    if not _DATED_NAME.fullmatch(name):
        return None
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def plan_prune(names: Iterable[str], today: date, keep_days: int, keep_min: int) -> PrunePlan:
    dated: dict[date, str] = {}
    ignored: list[str] = []
    for name in names:
        day = dated_name(name)
        if day is None:
            ignored.append(name)
        else:
            dated[day] = name
    newest_first = sorted(dated, reverse=True)
    try:
        cutoff = today - timedelta(days=keep_days)
    except OverflowError:
        # Further back than any date: nothing is old enough to purge.
        cutoff = date.min
    # Always keep the newest folders, so a clock far ahead can't make everything look old.
    candidates = newest_first[max(keep_min, 0) :]
    purge = sorted(day for day in candidates if day < cutoff)
    purged = set(purge)
    keep = [day for day in newest_first if day not in purged]
    return PrunePlan(
        purge=[dated[day] for day in purge],
        keep=[dated[day] for day in keep],
        ignored=sorted(ignored),
        cutoff=cutoff,
    )


def parse_listing(output: str) -> list[str]:
    """Folder names from ``rclone lsf --dirs-only``: one per line, ending in a slash.

    A line without the slash is a file (the job's flags may turn --dirs-only off): never a
    folder to purge.
    """
    names = []
    for line in output.split("\n"):
        line = line.removesuffix("\r")
        if line.endswith("/") and line != "/":
            names.append(line.removesuffix("/"))
    return names


async def run_prune(
    argv: list[str], env: dict[str, str], log_path: Path, today: date, timezone: str
) -> int:
    """Run a stored prune-dated command, logging to log_path. Returns an exit code.

    env is rclone's whole environment. today is the run's date in timezone.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:

        def note(text: str) -> None:
            log.write(f"{LOG_PREFIX} {text}\n".encode())
            log.flush()

        try:
            command = from_argv(argv)
        except PruneCommandError as exc:
            note(f"Refusing to run: {exc}. Nothing was purged.")
            return 2
        flags = rclone_flags(command.rclone_flags)
        # From all the flags: a dropped filter's value may be a --dry-run rclone would read.
        # RCLONE_DRY_RUN makes rclone purge nothing, so say so instead of "Purged".
        dry_run = command.dry_run or (
            env.get("RCLONE_DRY_RUN", "false").lower() not in _FALSE_VALUES
        )
        env = rclone_environment(env)

        note(f"Listing {command.path}")
        list_argv = ["rclone", "lsf", *_LIST_FLAGS, *flags, "--", command.path]
        exit_code, output = await _run(list_argv, env, log, capture=True)
        if exit_code == RCLONE_DIRECTORY_NOT_FOUND:
            note(f"{command.path} doesn't exist yet. Nothing to prune.")
            return 0
        if exit_code != 0:
            note(f"Listing failed with exit code {exit_code}. Nothing was purged.")
            return exit_code

        plan = plan_prune(
            parse_listing(output.decode(errors="replace")),
            today,
            command.keep_days,
            command.keep_min,
        )
        note(
            f"Today is {today.isoformat()} ({timezone}): purging dated folders before "
            f"{plan.cutoff.isoformat()} ({_days(command.keep_days)}), always keeping the "
            f"newest {command.keep_min}."
        )
        if plan.ignored:
            shown = ", ".join(repr(name) for name in plan.ignored[:LOGGED_IGNORED_NAMES])
            more = len(plan.ignored) - LOGGED_IGNORED_NAMES
            note(
                f"Leaving alone {len(plan.ignored)} other folder(s) not named YYYY-MM-DD: {shown}"
                + (f" and {more} more" if more > 0 else "")
            )
        if plan.keep:
            note(f"Keeping {len(plan.keep)} dated folder(s), {plan.keep[-1]} to {plan.keep[0]}")
        else:
            note("No dated folders found.")
        if not plan.purge:
            note("Nothing to purge.")
            return 0

        if dry_run:
            for name in plan.purge:
                note(f"Would purge {command.target(name)}")
            note(f"Dry run: would purge {len(plan.purge)} folder(s). Nothing was purged.")
            return 0

        for index, name in enumerate(plan.purge):
            target = command.target(name)
            note(f"Purging {target}")
            exit_code, _output = await _run(
                ["rclone", "purge", *_PURGE_FLAGS, *flags, "--", target], env, log, capture=False
            )
            if exit_code != 0:
                note(
                    f"Purging {target} failed with exit code {exit_code}. Stopped after "
                    f"purging {index} of {len(plan.purge)} folder(s)."
                )
                return exit_code
            note(f"Purged {target}")
        note(f"Done: purged {len(plan.purge)} folder(s), kept {len(plan.keep)}.")
        return 0


async def _run(argv: list[str], env: dict[str, str], log, capture: bool) -> tuple[int, bytes]:
    """Run rclone with its messages going to the log, and its output too unless captured."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture else log,
        stderr=log,
        env=env,
    )
    try:
        output, _ = await process.communicate()
        return process.returncode, output or b""
    except BaseException as exc:
        cancelled = await run_to_completion(stop_process(process, TERMINATE_TIMEOUT_SECONDS))
        if cancelled and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError from exc
        raise


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"

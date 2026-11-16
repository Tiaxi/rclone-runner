from __future__ import annotations

import os
import re
import shlex
import shutil
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core import prune


class CommandPolicyError(ValueError):
    """Raised when a command is outside the allowed rclone-only policy."""


# The app's own settings, including the session secret and the admin password hash. They are
# kept out of ${VAR} expansion and out of the environment rclone and console commands get.
APP_ENV_PREFIX = "RCLONE_RUNNER_"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_RCLONE_SUBCOMMANDS = {
    "about",
    "backend",
    "bisync",
    "cat",
    "check",
    "checksum",
    "cleanup",
    "config",
    "copy",
    "copyto",
    "cryptcheck",
    "cryptdecode",
    "delete",
    "deletefile",
    "dedupe",
    "genautocomplete",
    "gendocs",
    "hashsum",
    "link",
    "listremotes",
    "ls",
    "lsd",
    "lsf",
    "lsjson",
    "lsl",
    "md5sum",
    "mkdir",
    "mount",
    "move",
    "moveto",
    "ncdu",
    "obscure",
    "purge",
    "rc",
    "rcat",
    "rcd",
    "rmdir",
    "rmdirs",
    "selfupdate",
    "serve",
    "settier",
    "sha1sum",
    "size",
    "sync",
    "test",
    "touch",
    "tree",
    "version",
}

# Subcommands a dry run may start: they only read, or rclone checks --dry-run before each change
# they make. The others make changes anyway (config delete deletes the remote, gendocs writes
# its files, bisync writes its listings to its working directory) or could not be shown not
# to, so a dry run refuses them.
_DRY_RUN_SUBCOMMANDS = {
    "about",
    "cat",
    "check",
    "checksum",
    "copy",
    "copyto",
    "cryptcheck",
    "cryptdecode",
    "dedupe",
    "delete",
    "deletefile",
    "hashsum",
    "listremotes",
    "ls",
    "lsd",
    "lsf",
    "lsjson",
    "lsl",
    "md5sum",
    "mkdir",
    "move",
    "moveto",
    "obscure",
    "purge",
    "rmdir",
    "rmdirs",
    "sha1sum",
    "size",
    "sync",
    "touch",
    "tree",
    "version",
}
# Subcommands with their own subcommands, of which a dry run may start only these.
_DRY_RUN_NESTED = {
    "backend": {"features", "help"},
    "config": {"dump", "file", "paths", "providers", "redacted", "show"},
}


def run_variables(started_at: datetime, timezone: str) -> dict[str, str]:
    """Variables for ${...} in job steps, fixed for the whole run, in the app's time zone.

    For example ``--backup-dir remote:versions/${RUN_DATE}`` keeps each day's replaced and
    deleted files apart. A job's own environment line with the same name wins, for
    prune-dated too (see effective_run_date).
    """
    local = _local(started_at, timezone)
    return {"RUN_DATE": local.strftime("%Y-%m-%d"), "RUN_TIME": local.strftime("%H-%M-%S")}


def run_date(started_at: datetime, timezone: str) -> date:
    """The run's start date in the app's time zone, as ${RUN_DATE} gives it by default."""
    return _local(started_at, timezone).date()


_RUN_DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def effective_run_date(env: dict[str, str]) -> date:
    """The date ${RUN_DATE} stands for in this run: its start date, or the job's own RUN_DATE.

    prune-dated counts days from it, so that it can't pick the folder a backup with
    --backup-dir .../${RUN_DATE} has just written as an old one. Raises CommandPolicyError
    unless it is a YYYY-MM-DD date.
    """
    value = env.get("RUN_DATE", "")
    if _RUN_DATE_FORMAT.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CommandPolicyError(
        f"{prune.PRUNE_COMMAND} counts days from ${{RUN_DATE}}, which must be a date such as "
        f"2026-11-16; the job sets RUN_DATE={value!r}"
    )


def _local(started_at: datetime, timezone: str) -> datetime:
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return started_at.astimezone(ZoneInfo(timezone))


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for rclone: the container's, without the app's settings, plus extra."""
    inherited = {
        key: value for key, value in os.environ.items() if not key.startswith(APP_ENV_PREFIX)
    }
    return inherited | (extra or {})


def _expand_env(value: str, env: dict[str, str]) -> str:
    merged = child_environment(env)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in merged:
            raise CommandPolicyError(f"missing environment variable: {name}")
        return merged[name]

    return _ENV_PATTERN.sub(replace, value)


def _split(value: str, env: dict[str, str]) -> list[str]:
    expanded = _expand_env(value, env)
    try:
        return shlex.split(expanded)
    except ValueError as exc:
        raise CommandPolicyError(str(exc)) from exc


def build_step_argv(step_command: str, common_args: str, env: dict[str, str]) -> list[str]:
    """A job step's command: an rclone command, or a built-in such as prune-dated."""
    step_parts = _split(step_command, env)
    if step_parts and step_parts[0] == prune.PRUNE_COMMAND:
        try:
            command = prune.parse_step(step_parts[1:], _split(common_args, env))
        except prune.PruneCommandError as exc:
            raise CommandPolicyError(f"{prune.PRUNE_COMMAND}: {exc}") from exc
        return command.argv()
    return _rclone_argv(step_parts, common_args, env)


def build_rclone_argv(step_command: str, common_args: str, env: dict[str, str]) -> list[str]:
    return _rclone_argv(_split(step_command, env), common_args, env)


def _rclone_argv(step_parts: list[str], common_args: str, env: dict[str, str]) -> list[str]:
    if not step_parts:
        raise CommandPolicyError("step command is empty")
    if step_parts[0] == "rclone":
        step_parts = step_parts[1:]
    if not step_parts:
        raise CommandPolicyError("rclone subcommand is missing")
    if step_parts[0] not in _ALLOWED_RCLONE_SUBCOMMANDS:
        raise CommandPolicyError(f"unsupported rclone subcommand: {step_parts[0]}")

    return ["rclone", step_parts[0], *_split(common_args, env), *step_parts[1:]]


def requests_dry_run(argv: list[str], env: dict[str, str]) -> bool:
    """Whether rclone might run this command as a dry run: by --dry-run or -n in it, or by
    RCLONE_DRY_RUN in its environment (the container's, then the job's).

    The arguments decide as rclone reads them, the last one setting it winning, when rclone's
    flag list tells which arguments are flags and which their values. Without it, or with a
    flag it doesn't list (whose value could be anything that follows), any argument that could
    turn a dry run on counts: what looks like --dry-run=false may be another flag's value
    (--exclude --dry-run=false) and leave an earlier --dry-run on. That only ever means
    checking the command.
    """
    from app.core import sources

    value = child_environment(env).get("RCLONE_DRY_RUN")
    # rclone reads the variable as a boolean: only these are false; anything else is true or
    # stops rclone from starting.
    from_env = value is not None and value not in _FALSE_BOOLS
    if prune.is_prune_command(argv):
        # Its own options aren't rclone's: what counts is the rclone lsf and purge it runs,
        # with the flags it gives them.
        try:
            command = prune.from_argv(argv)
        except prune.PruneCommandError:
            return True
        return command.dry_run or requests_dry_run(
            ["rclone", "lsf", *prune.rclone_flags(command.rclone_flags)], env
        )
    if not any(_may_set_dry_run(arg) for arg in argv[2:]):
        return from_env
    if not sources.flags_available():
        return True
    known = sources._flags("global").names | sources._flags(argv[1]).names
    flags, _paths = sources.split_command(argv)
    setting = None
    for flag in flags:
        name = flag[0].partition("=")[0]
        if name not in known and "=" not in flag[0] and len(flag) == 1:
            return True  # It may take the next argument as its value: then it isn't clear.
        if name in {"--dry-run", "-n"}:
            setting = prune.dry_run_requested(flag[:1])
    return from_env if setting is None else setting


# The values rclone's flag parser reads as false (Go's strconv.ParseBool).
_FALSE_BOOLS = {"0", "f", "F", "false", "FALSE", "False"}
# The letters at the start of a group of short flags, such as -vn or -nf- *.tmp (-n, then -f
# with the value "- *.tmp"): a value can follow the first letter that takes one.
_SHORT_LETTERS = re.compile(r"-([A-Za-z0-9]+)")


def _may_set_dry_run(arg: str) -> bool:
    if arg.partition("=")[0] == "--dry-run":
        return True
    match = _SHORT_LETTERS.match(arg)
    return match is not None and not arg.startswith("--") and "n" in match[1]


def dry_run_refusal(argv: list[str], env: dict[str, str]) -> str | None:
    """Why this command must not run as a dry run, or None if --dry-run keeps it from changing
    anything.

    Takes the whole command as build_step_argv builds it, common arguments included: they can
    select a nested subcommand ("config show" with "--config x delete" runs config delete) or
    add a flag that writes a file.
    """
    creating = _creating_option(argv, env)
    if creating is not None:
        return f"a dry run can't start: {creating} can create data on the remote even in a dry run"
    if prune.is_prune_command(argv):
        # It handles dry runs itself, listing with rclone lsf: whose flags, the step's and
        # the common arguments, must not write a file either.
        writer = _file_writing_flag(["rclone", "lsf", *argv[1:]], env)
        if writer is not None:
            return f"a dry run can't start: {writer} writes a file even with --dry-run"
        return None
    subcommand = argv[1]
    if subcommand in _DRY_RUN_NESTED:
        allowed = _DRY_RUN_NESTED[subcommand]
        refused = [name for name in _nested_subcommands(argv[2:]) if name not in allowed]
        if refused:
            # None: no nested subcommand, which runs the interactive "rclone config".
            name = sorted(refused, key=lambda item: item or "")[0]
            command = f"{subcommand} {name}" if name else subcommand
            return f"a dry run can't start: rclone {command} can make changes even with --dry-run"
    elif subcommand not in _DRY_RUN_SUBCOMMANDS:
        return f"a dry run can't start: rclone {subcommand} can make changes even with --dry-run"
    writer = _file_writing_flag(argv, env)
    if writer is not None:
        return f"a dry run can't start: {writer} writes a file even with --dry-run"
    return None


# Backend options that make rclone create data on a remote just by reading it, and the flags that
# turn them off. A dry run is started with these flags, which beat the config file and the
# environment; only a connection string (remote,create_library=true:) or the job's own flag,
# later on the command line, would beat them, so a dry run refuses those.
DRY_RUN_SAFEGUARDS = ("--seafile-create-library=false",)
_CREATING_OPTIONS = ("create_library", "create-library")


# The option anywhere in a value: a connection string's parameter, which beats any flag
# (s,create_library=true:library, or bare, s,create_library:library, which means true).
_CREATING_PARAMETER = re.compile(r"create[_-]library", re.IGNORECASE)
# The option set on its own line of a remote's section: the dry run's flag beats that.
_CREATING_KEY = re.compile(r"\s*create[_-]library\s*=", re.IGNORECASE)
# A value built from others (%(name)s), which the check can't follow.
_INTERPOLATION = "%("
_ENCRYPTED_CONFIG = "# Encrypted rclone configuration File"


def _creating_option(argv: list[str], env: dict[str, str]) -> str | None:
    """A setting that would let a backend create data on the remote in a dry run, despite
    DRY_RUN_SAFEGUARDS: in the command, or as a connection string's parameter in the
    environment or in rclone's config file, where a wrapper remote such as an alias can name
    one ("remote = s,create_library=true:library"). An encrypted config can't be read."""
    for arg in argv:
        if arg in DRY_RUN_SAFEGUARDS:
            continue
        if any(option in arg.lower() for option in _CREATING_OPTIONS):
            return "Seafile's create_library"
    rclone_env = child_environment(env)
    if any(_CREATING_PARAMETER.search(value) for value in rclone_env.values()):
        return "Seafile's create_library"
    for path in _config_files(argv, rclone_env):
        if _config_names_creation(path):
            return "Seafile's create_library"
    return None


def _config_names_creation(path: str) -> bool:
    """Whether an rclone config file, read whole, may give a connection string the option:
    the option anywhere but on its own line, or a value built from others, which can spell it
    out. An encrypted one can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as config:
            for number, line in enumerate(config):
                if number == 0 and line.startswith(_ENCRYPTED_CONFIG):
                    return False
                if line.lstrip().startswith(("#", ";")) or _CREATING_KEY.match(line):
                    continue
                if _CREATING_PARAMETER.search(line) or _INTERPOLATION in line:
                    return True
    except OSError:
        pass
    return False


def _config_files(argv: list[str], env: dict[str, str]) -> list[str]:
    """Every rclone config file a command may read: each --config it mentions, wherever, even
    as another flag's value (which one rclone takes depends on how it reads the arguments),
    RCLONE_CONFIG, and the places rclone looks by default. Checking them all is the safe side.
    """
    files = []
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            files.append(argv[index + 1])
        elif arg.startswith("--config="):
            files.append(arg.split("=", 1)[1])
    if env.get("RCLONE_CONFIG"):
        files.append(env["RCLONE_CONFIG"])
    home = env.get("HOME") or os.path.expanduser("~")
    if env.get("XDG_CONFIG_HOME"):
        files.append(os.path.join(env["XDG_CONFIG_HOME"], "rclone", "rclone.conf"))
    files.append(os.path.join(home, ".config", "rclone", "rclone.conf"))
    files.append(os.path.join(home, ".rclone.conf"))
    executable = shutil.which("rclone", path=env.get("PATH"))
    if executable:
        files.append(os.path.join(os.path.dirname(os.path.realpath(executable)), "rclone.conf"))
    return files


def with_dry_run_safeguards(argv: list[str], env: dict[str, str]) -> list[str]:
    """The command to start for a step that may be a dry run, with DRY_RUN_SAFEGUARDS."""
    if not requests_dry_run(argv, env):
        return argv
    if prune.is_prune_command(argv):
        # Its rclone flags come last, and go to rclone lsf.
        return [*argv, *DRY_RUN_SAFEGUARDS]
    return [*argv[:2], *DRY_RUN_SAFEGUARDS, *argv[2:]]


# Flags that make an otherwise dry-run-safe command write a file: reports, hash lists,
# profiles, and rclone's own log, which can also rotate and delete older logs.
_FILE_WRITING_FLAGS = (
    "--combined",
    "--cpuprofile",
    "--dest-after",
    "--differ",
    "--error",
    "--log-file",
    "--match",
    "--memprofile",
    "--missing-on-dst",
    "--missing-on-src",
    "--output-file",
)
# tree's -o, --output.
_TREE_OUTPUT = "--output"


def _file_writing_flag(argv: list[str], env: dict[str, str]) -> str | None:
    """A flag in the command, or an RCLONE_* variable setting one, that writes a file.

    Any argument that looks like one counts, even another flag's value: that only ever means
    refusing a dry run.
    """
    flags = list(_FILE_WRITING_FLAGS)
    if argv[1] == "tree":
        flags.append(_TREE_OUTPUT)
    for arg in argv[2:]:
        name = arg.partition("=")[0]
        if name in flags:
            return name
        if argv[1] == "tree" and arg.startswith("-") and not arg.startswith("--") and "o" in arg:
            return "-o"
    rclone_env = child_environment(env)
    for flag in flags:
        variable = "RCLONE_" + flag[2:].upper().replace("-", "_")
        if rclone_env.get(variable):
            return variable
    return None


# Flags known not to take a value, so they can't hide a nested subcommand behind them: the
# --dry-run a dry run adds, and common switches. With rclone's flag list loaded, it decides.
_KNOWN_SWITCHES = frozenset(
    {"--dry-run", "-n", "--verbose", "-v", "--quiet", "-q", "--progress", "-P", "--fast-list"}
)


def _nested_subcommands(args: list[str]) -> set[str | None]:
    """The subcommands of config or backend these arguments may select, as rclone's command
    parser (cobra) finds them: the first argument left after removing flags and their values.

    cobra takes "--flag x" and "-f x" as a flag with a value unless it knows the flag as a
    switch. Without rclone's flag list it isn't known which flags those are, so every reading
    counts; None stands for finding no subcommand.
    """
    from app.core import sources

    if sources.flags_available():
        flag_set = sources._flags("global")

        def is_switch(flag: str) -> bool | None:
            return flag in flag_set.names and flag not in flag_set.with_values
    else:

        def is_switch(flag: str) -> bool | None:
            return True if flag in _KNOWN_SWITCHES else None

    found: set[str | None] = set()

    def walk(index: int) -> None:
        while index < len(args):
            arg = args[index]
            index += 1
            if arg == "--":
                break
            takes_next = "=" not in arg and (
                arg.startswith("--") or (arg.startswith("-") and len(arg) == 2)
            )
            if takes_next:
                switch = is_switch(arg)
                if switch:
                    continue
                if switch is None:
                    walk(index)  # The reading where it is a switch.
                if len(args) - index <= 1:
                    break  # cobra stops here, with no subcommand.
                index += 1
                continue
            if arg and not arg.startswith("-"):
                found.add(arg)
                return
        found.add(None)

    walk(0)
    return found


def parse_console_command(command: str) -> list[str]:
    parts = _split(command, {})
    if not parts:
        raise CommandPolicyError("command is empty")
    if parts[0] == "rclone":
        parts = parts[1:]
    if not parts:
        raise CommandPolicyError("rclone subcommand is missing")
    if parts == ["--version"]:
        return ["rclone", "version"]
    if parts[0] not in _ALLOWED_RCLONE_SUBCOMMANDS:
        raise CommandPolicyError("only rclone commands are allowed")
    return ["rclone", *parts]

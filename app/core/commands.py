from __future__ import annotations

import os
import re
import shlex
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


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


def run_variables(started_at: datetime, timezone: str) -> dict[str, str]:
    """Variables for ${...} in job steps, fixed for the whole run, in the app's time zone.

    For example ``--backup-dir remote:versions/${RUN_DATE}`` keeps each day's replaced and
    deleted files apart. A job's own environment line with the same name wins.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    local = started_at.astimezone(ZoneInfo(timezone))
    return {"RUN_DATE": local.strftime("%Y-%m-%d"), "RUN_TIME": local.strftime("%H-%M-%S")}


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


def build_rclone_argv(step_command: str, common_args: str, env: dict[str, str]) -> list[str]:
    step_parts = _split(step_command, env)
    if not step_parts:
        raise CommandPolicyError("step command is empty")
    if step_parts[0] == "rclone":
        step_parts = step_parts[1:]
    if not step_parts:
        raise CommandPolicyError("rclone subcommand is missing")
    if step_parts[0] not in _ALLOWED_RCLONE_SUBCOMMANDS:
        raise CommandPolicyError(f"unsupported rclone subcommand: {step_parts[0]}")

    return ["rclone", step_parts[0], *_split(common_args, env), *step_parts[1:]]


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

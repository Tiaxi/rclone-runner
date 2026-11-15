from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_KEY = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RCLONE_RUNNER_")

    admin_user: str = "admin"
    admin_password_hash: str = ""
    secret_key: str = DEFAULT_SECRET_KEY
    data_dir: Path = Path("data")
    log_dir: Path = Path("data/logs")
    database_url: str = "sqlite:///data/rclone-runner.db"
    timezone: str = "Europe/Helsinki"
    retention_days: int = 30


def unwritable_paths(value: Settings) -> list[Path]:
    """Paths the app must write to that the current user cannot write."""
    paths = [value.data_dir, value.log_dir]
    database_path = _sqlite_path(value.database_url)
    if database_path is not None:
        paths.append(database_path.parent)
        # SQLite also writes -wal/-shm (and -journal) files next to the database.
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = database_path.with_name(database_path.name + suffix)
            if candidate.exists():
                paths.append(candidate)
    problems: list[Path] = []
    for path in dict.fromkeys(paths):
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        # Folders also need search (x) permission to create and open files inside them.
        mode = os.W_OK | os.X_OK if target.is_dir() else os.W_OK
        if not os.access(target, mode):
            problems.append(path)
    return problems


def startup_permission_error(value: Settings) -> str | None:
    problems = unwritable_paths(value)
    if not problems:
        return None
    uid, gid = os.getuid(), os.getgid()
    listed = ", ".join(str(path) for path in problems)
    return (
        f"Rclone Runner runs as uid {uid} (gid {gid}) and cannot write to: {listed}. "
        f"Make the host folders mounted there owned by this user, for example "
        f"`chown -R {uid}:{gid} <host data folder> <host rclone config folder>`, "
        "or set RCLONE_RUNNER_UID/RCLONE_RUNNER_GID to their owner."
    )


def _sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


def runtime_warnings(value: Settings) -> list[str]:
    warnings: list[str] = []
    if not value.admin_password_hash:
        warnings.append(
            "RCLONE_RUNNER_ADMIN_PASSWORD_HASH is not set; "
            "the development admin password is enabled."
        )
    secret_key = value.secret_key.strip()
    if not secret_key or secret_key in {DEFAULT_SECRET_KEY, "change-this-long-random-string"}:
        warnings.append(
            "RCLONE_RUNNER_SECRET_KEY uses a default session secret; set a long random value."
        )
    rclone_config = os.environ.get("RCLONE_CONFIG")
    if rclone_config and not os.access(Path(rclone_config).parent, os.W_OK):
        warnings.append(
            f"The rclone config folder {Path(rclone_config).parent} is not writable; rclone "
            "cannot save renewed tokens or config changes."
        )
    return warnings


settings = Settings()

from __future__ import annotations

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


def runtime_warnings(value: Settings) -> list[str]:
    warnings: list[str] = []
    if not value.admin_password_hash:
        warnings.append(
            "RCLONE_RUNNER_ADMIN_PASSWORD_HASH is not set; "
            "the development admin password is enabled."
        )
    if value.secret_key in {DEFAULT_SECRET_KEY, "change-this-long-random-string"}:
        warnings.append(
            "RCLONE_RUNNER_SECRET_KEY uses a default session secret; "
            "set a long random value."
        )
    return warnings


settings = Settings()

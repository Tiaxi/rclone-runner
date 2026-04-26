from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RCLONE_RUNNER_")

    admin_user: str = "admin"
    admin_password_hash: str = ""
    secret_key: str = "change-me"
    data_dir: Path = Path("data")
    log_dir: Path = Path("data/logs")
    database_url: str = "sqlite:///data/rclone-runner.db"
    timezone: str = "Europe/Helsinki"
    retention_days: int = 30


settings = Settings()

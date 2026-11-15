import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.auth import (
    CSRF_SESSION_KEY,
    LoginThrottle,
    csrf_field,
    hash_password,
    require_csrf,
    verify_password_hash,
)
from app.config import Settings, runtime_warnings, startup_permission_error


class SessionRequest:
    def __init__(self) -> None:
        self.session = {}


async def test_csrf_field_creates_stable_session_token():
    request = SessionRequest()

    first = csrf_field(request)
    second = csrf_field(request)

    assert first == second
    assert f'name="csrf_token" value="{request.session[CSRF_SESSION_KEY]}"' in first


async def test_require_csrf_rejects_invalid_token():
    request = SessionRequest()
    request.session[CSRF_SESSION_KEY] = "expected"

    with pytest.raises(HTTPException) as exc_info:
        await require_csrf(request, csrf_token="wrong")

    assert exc_info.value.status_code == 403


def test_password_hash_verifies_matching_password():
    password_hash = hash_password("correct horse battery staple")

    assert verify_password_hash("correct horse battery staple", password_hash)
    assert not verify_password_hash("wrong", password_hash)


def test_password_hash_rejects_unknown_format():
    assert not verify_password_hash("password", "bcrypt$not-supported")


def test_runtime_warnings_flag_missing_password_hash_and_default_secret():
    warnings = runtime_warnings(Settings(admin_password_hash="", secret_key="change-me"))

    assert "development admin password" in warnings[0]
    assert "default session secret" in warnings[1]


def test_runtime_warnings_are_empty_for_hardened_config():
    warnings = runtime_warnings(Settings(admin_password_hash="hash", secret_key="not-default"))

    assert warnings == []


def test_runtime_warnings_flag_blank_session_secret():
    warnings = runtime_warnings(Settings(admin_password_hash="hash", secret_key="   "))

    assert any("default session secret" in warning for warning in warnings)


def test_login_throttle_blocks_after_limit_and_expires():
    now = [1000.0]
    throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=lambda: now[0])

    for _ in range(3):
        assert throttle.retry_after("10.0.0.5") is None
        throttle.record_failure("10.0.0.5")

    assert throttle.retry_after("10.0.0.5") == 60
    assert throttle.retry_after("10.0.0.6") is None
    now[0] += 61
    assert throttle.retry_after("10.0.0.5") is None


def test_login_throttle_reset_clears_failures():
    throttle = LoginThrottle(max_failures=1, window_seconds=60, clock=lambda: 0.0)
    throttle.record_failure("10.0.0.5")

    throttle.reset("10.0.0.5")

    assert throttle.retry_after("10.0.0.5") is None


needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")


@needs_non_root
def test_startup_permission_error_names_unwritable_data_folder(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o555)
    try:
        value = Settings(
            data_dir=data_dir,
            log_dir=data_dir / "logs",
            database_url=f"sqlite:///{data_dir}/rclone-runner.db",
        )

        message = startup_permission_error(value)
    finally:
        data_dir.chmod(0o755)

    assert message is not None
    assert str(data_dir) in message
    assert f"chown -R {os.getuid()}:{os.getgid()}" in message


def test_startup_permission_error_is_none_for_writable_folders(tmp_path: Path):
    value = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "data" / "logs",
        database_url=f"sqlite:///{tmp_path}/data/rclone-runner.db",
    )

    assert startup_permission_error(value) is None


@needs_non_root
def test_runtime_warnings_flag_unwritable_rclone_config_folder(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "rclone"
    config_dir.mkdir()
    config_dir.chmod(0o555)
    monkeypatch.setenv("RCLONE_CONFIG", str(config_dir / "rclone.conf"))
    try:
        warnings = runtime_warnings(Settings(admin_password_hash="hash", secret_key="x" * 40))
    finally:
        config_dir.chmod(0o755)

    assert any("not writable" in warning for warning in warnings)


def test_login_throttle_caps_tracked_clients():
    now = [0.0]
    throttle = LoginThrottle(
        max_failures=1, window_seconds=600, clock=lambda: now[0], max_clients=2
    )

    for client in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        now[0] += 1
        throttle.record_failure(client)

    assert throttle.retry_after("10.0.0.1") is None
    assert throttle.retry_after("10.0.0.2") is not None
    assert throttle.retry_after("10.0.0.3") is not None


@needs_non_root
def test_startup_permission_error_flags_unwritable_sqlite_wal_and_untraversable_folder(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "rclone-runner.db"
    database.write_bytes(b"")
    wal = data_dir / "rclone-runner.db-wal"
    wal.write_bytes(b"")
    wal.chmod(0o444)
    logs = data_dir / "logs"
    logs.mkdir()
    logs.chmod(0o666)
    try:
        message = startup_permission_error(
            Settings(data_dir=data_dir, log_dir=logs, database_url=f"sqlite:///{database}")
        )
    finally:
        logs.chmod(0o755)
        wal.chmod(0o644)

    assert message is not None
    assert str(wal) in message
    assert str(logs) in message

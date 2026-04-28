import pytest
from fastapi import HTTPException

from app.auth import CSRF_SESSION_KEY, csrf_field, hash_password, require_csrf, verify_password_hash
from app.config import Settings, runtime_warnings


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

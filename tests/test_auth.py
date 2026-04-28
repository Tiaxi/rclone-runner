from app.auth import hash_password, verify_password_hash
from app.config import Settings, runtime_warnings


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

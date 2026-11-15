import pytest

from app.core.commands import (
    CommandPolicyError,
    build_rclone_argv,
    child_environment,
    parse_console_command,
)


def test_builds_rclone_command_with_common_args_after_operation():
    argv = build_rclone_argv(
        step_command="sync /media/Musiikki secret:/Musiikki",
        common_args="--fast-list --transfers=20 --bwlimit ${BW_LIMIT}",
        env={"BW_LIMIT": "8M"},
    )

    assert argv == [
        "rclone",
        "sync",
        "--fast-list",
        "--transfers=20",
        "--bwlimit",
        "8M",
        "/media/Musiikki",
        "secret:/Musiikki",
    ]


def test_shell_metacharacters_are_passed_as_literals():
    argv = build_rclone_argv(
        step_command="lsjson secret:/Folder ';' rm -rf /",
        common_args="",
        env={},
    )

    assert argv == ["rclone", "lsjson", "secret:/Folder", ";", "rm", "-rf", "/"]


def test_rejects_empty_step_command():
    with pytest.raises(CommandPolicyError):
        build_rclone_argv(step_command=" ", common_args="", env={})


def test_console_accepts_shorthand_rclone_subcommand():
    assert parse_console_command("config show secret:") == [
        "rclone",
        "config",
        "show",
        "secret:",
    ]


def test_console_accepts_explicit_rclone_command():
    assert parse_console_command("rclone lsd secret:") == ["rclone", "lsd", "secret:"]


def test_console_accepts_rclone_version_flag_as_version_command():
    assert parse_console_command("rclone --version") == ["rclone", "version"]


def test_console_rejects_non_rclone_commands():
    with pytest.raises(CommandPolicyError):
        parse_console_command("bash")


def test_app_settings_are_not_expanded_into_commands(monkeypatch):
    monkeypatch.setenv("RCLONE_RUNNER_SECRET_KEY", "session-secret")
    monkeypatch.setenv("SHARED_REMOTE", "holvi")

    with pytest.raises(CommandPolicyError, match="RCLONE_RUNNER_SECRET_KEY"):
        build_rclone_argv("lsd ${RCLONE_RUNNER_SECRET_KEY}:", "", {})
    assert build_rclone_argv("lsd ${SHARED_REMOTE}:", "", {}) == ["rclone", "lsd", "holvi:"]


def test_child_environment_drops_app_settings_and_adds_job_values(monkeypatch):
    monkeypatch.setenv("RCLONE_RUNNER_ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("RCLONE_CONFIG", "/config/rclone/rclone.conf")

    env = child_environment({"BW_LIMIT": "8M"})

    assert "RCLONE_RUNNER_ADMIN_PASSWORD_HASH" not in env
    assert env["RCLONE_CONFIG"] == "/config/rclone/rclone.conf"
    assert env["BW_LIMIT"] == "8M"


def test_run_variables_use_the_app_time_zone():
    from datetime import UTC, datetime

    from app.core.commands import run_variables

    # 22:30 UTC is already the next day in Helsinki.
    late = datetime(2026, 11, 14, 22, 30, 5, tzinfo=UTC)
    assert run_variables(late, "Europe/Helsinki") == {
        "RUN_DATE": "2026-11-15",
        "RUN_TIME": "00-30-05",
    }
    # Database times are naive UTC.
    assert run_variables(late.replace(tzinfo=None), "UTC")["RUN_DATE"] == "2026-11-14"


def test_run_date_expands_in_steps_and_common_args():
    argv = build_rclone_argv(
        "sync /media/Musa crypt:current/Musa",
        "--backup-dir crypt:versions/${RUN_DATE}/Musa",
        {"RUN_DATE": "2026-11-15", "RUN_TIME": "05-00-00"},
    )

    assert "crypt:versions/2026-11-15/Musa" in argv

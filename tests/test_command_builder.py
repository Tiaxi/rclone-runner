import pytest

from app.core.commands import CommandPolicyError, build_rclone_argv, parse_console_command


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

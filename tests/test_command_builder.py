import pytest

from app.core.commands import (
    CommandPolicyError,
    build_rclone_argv,
    build_step_argv,
    child_environment,
    dry_run_refusal,
    parse_console_command,
    requests_dry_run,
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


def _refusal(command: str, common: str = "", env=None):
    env = env or {}
    return dry_run_refusal(build_step_argv(command, common, env), env)


@pytest.mark.parametrize(
    "command",
    [
        "sync /a crypt:a",
        "rclone copy /a crypt:a",
        "delete crypt:old",
        "lsf crypt:",
        "hashsum MD5 crypt:",
        "tree crypt: --level 2",
        "config show",
        "config dump",
        "backend features crypt:",
        "prune-dated crypt:versions --keep-days 30 --keep-min 2",
    ],
)
def test_dry_run_starts_commands_that_honour_dry_run(command):
    assert _refusal(command) is None
    assert _refusal(command, "--dry-run --fast-list") is None


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("config delete example", "config delete"),
        ("rclone config create other local", "config create"),
        ("config", "config"),
        ("backend set crypt: -o x=y", "backend set"),
        ("gendocs /tmp/docs", "gendocs"),
        ("test makefiles /tmp/x", "test"),
        ("link crypt:file", "link"),
        ("rc operations/purge", "rc"),
        ("serve webdav crypt:", "serve"),
        ("selfupdate", "selfupdate"),
        # It writes its listings to its working directory even in a dry run.
        ("bisync /a crypt:a --resync", "bisync"),
    ],
)
def test_dry_run_refuses_commands_that_change_things_anyway(command, name):
    assert _refusal(command) == (
        f"a dry run can't start: rclone {name} can make changes even with --dry-run"
    )


def test_dry_run_finds_the_nested_subcommand_the_common_arguments_select():
    # cobra takes --config's value, then "delete": rclone config delete show.
    assert "rclone config delete" in _refusal("config show", "--config /tmp/x.conf delete")
    assert "rclone config delete" in _refusal("config show", "--dry-run --config=x delete")
    # Flags before a read-only subcommand are fine once their values are told apart.
    assert _refusal("config --config /tmp/x.conf show") is None
    assert _refusal("config --dry-run show") is None
    # A value-taking flag with nothing after its value leaves no subcommand: interactive config.
    assert "rclone config can make" in _refusal("config --config /tmp/x.conf")


def test_dry_run_without_rclones_flag_list_refuses_ambiguous_nested_commands(monkeypatch):
    import app.core.sources as sources

    monkeypatch.setattr(sources, "_flag_sets", {})
    # --unknown may or may not take "show" as its value; either reading must be safe.
    assert "rclone config" in _refusal("config --unknown show delete")
    assert _refusal("config --dry-run show") is None
    assert _refusal("config --config=/tmp/x.conf show") is None


@pytest.mark.parametrize(
    ("command", "common", "flag"),
    [
        ("hashsum MD5 /src --output-file /tmp/existing", "", "--output-file"),
        ("md5sum /src", "--output-file=/tmp/existing", "--output-file"),
        ("check /a /b --combined /tmp/report", "", "--combined"),
        ("sync /a crypt:a", "--missing-on-dst /tmp/x", "--missing-on-dst"),
        ("copy /a crypt:a --dest-after /tmp/x", "", "--dest-after"),
        ("tree /src -o /tmp/tree.txt", "", "-o"),
        ("tree /src --output=/tmp/tree.txt", "", "--output"),
        ("lsf /src", "--log-file /tmp/rclone.log", "--log-file"),
        ("size /src", "--cpuprofile /tmp/cpu", "--cpuprofile"),
    ],
)
def test_dry_run_refuses_flags_that_write_files(command, common, flag):
    assert _refusal(command, common) == (
        f"a dry run can't start: {flag} writes a file even with --dry-run"
    )


def test_dry_run_refuses_file_writing_flags_set_in_the_environment():
    refusal = _refusal("check /a /b", env={"RCLONE_COMBINED": "/tmp/report"})
    assert refusal == "a dry run can't start: RCLONE_COMBINED writes a file even with --dry-run"


def test_requests_dry_run_reads_flags_and_environment():
    assert requests_dry_run(["rclone", "sync", "--dry-run", "/a", "b:"], {})
    assert requests_dry_run(["rclone", "sync", "-vn", "/a", "b:"], {})
    assert requests_dry_run(["rclone", "config", "delete", "x"], {"RCLONE_DRY_RUN": "true"})
    assert not requests_dry_run(["rclone", "config", "delete", "x"], {})
    assert not requests_dry_run(["rclone", "sync", "-v", "/a", "b:"], {})
    # As rclone reads them: the last setting wins, and another flag's value isn't one.
    assert not requests_dry_run(["rclone", "sync", "--dry-run=false", "/a", "b:"], {})
    assert requests_dry_run(["rclone", "sync", "--dry-run", "--exclude", "--dry-run=false"], {})
    assert not requests_dry_run(["rclone", "sync", "--exclude", "-n=false", "/a", "b:"], {})
    assert requests_dry_run(["rclone", "sync", "-nf- *.tmp", "/a", "b:"], {})
    assert not requests_dry_run(["rclone", "config", "delete", "x"], {"RCLONE_DRY_RUN": "false"})
    assert requests_dry_run(["rclone", "config", "delete", "x"], {"RCLONE_DRY_RUN": "no"})


def test_requests_dry_run_is_cautious_where_it_cant_tell(monkeypatch):
    import app.core.sources as sources

    # A flag rclone doesn't list might take "--dry-run=false" as its value.
    assert requests_dry_run(["rclone", "sync", "--dry-run", "--unknown", "--dry-run=false"], {})
    # Without rclone's flag list, any mention counts.
    monkeypatch.setattr(sources, "_flag_sets", {})
    assert requests_dry_run(["rclone", "sync", "--dry-run=false", "/a", "b:"], {})
    assert requests_dry_run(["rclone", "sync", "-nf- *.tmp", "/a", "b:"], {})

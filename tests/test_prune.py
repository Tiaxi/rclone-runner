import asyncio
import json
import os
import shutil
import signal
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.prune as prune_module
import app.core.runner as runner_module
from app.core.commands import CommandPolicyError, build_step_argv
from app.core.models import Job, JobStep
from app.core.prune import (
    PruneCommandError,
    check_path,
    dated_name,
    dry_run_requested,
    from_argv,
    may_turn_off_dry_run,
    parse_listing,
    plan_prune,
    rclone_environment,
    rclone_flags,
    run_prune,
)
from app.core.runner import JobRunner, LiveJobRunner
from app.core.sources import size_command
from app.db import Base, JobStepRunRecord

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")

# Folder names a versions folder may hold: dated ones, and others to leave alone.
DATED = ["2026-01-05", "2026-06-30", "2026-10-01", "2026-10-17", "2026-11-01", "2026-11-16"]
OTHERS = ["2026-02-30", "2026-1-05", "2026-01-05x", " 2026-01-05", "notes", "20260105"]


def test_parses_a_prune_step_with_common_args_and_its_own_flags():
    argv = build_step_argv(
        "prune-dated crypt:versions --keep-days=30 --keep-min 7 --b2-hard-delete=true",
        "--transfers=4 --bwlimit ${BW}",
        {"BW": "8M"},
    )

    assert argv == [
        "prune-dated",
        "crypt:versions",
        "--keep-days",
        "30",
        "--keep-min",
        "7",
        "--transfers=4",
        "--bwlimit",
        "8M",
        "--b2-hard-delete=true",
    ]
    command = from_argv(argv)
    assert (command.path, command.keep_days, command.keep_min) == ("crypt:versions", 30, 7)
    assert not command.dry_run
    assert command.description() == (
        "permanently deletes dated folders in crypt:versions older than 30 days, "
        "always keeping the newest 7"
    )


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ("prune-dated --keep-days 30 --keep-min 7", "takes one path, got 0"),
        ("prune-dated '' --keep-days 30 --keep-min 7", "the path is empty"),
        ("prune-dated crypt: --keep-days 30 --keep-min 7", "root of crypt:"),
        ("prune-dated crypt:/ --keep-days 30 --keep-min 7", "root of crypt:"),
        ("prune-dated crypt:./ --keep-days 30 --keep-min 7", "root of crypt:"),
        ("prune-dated / --keep-days 30 --keep-min 7", "root of the file system"),
        ("prune-dated // --keep-days 30 --keep-min 7", "root of the file system"),
        ("prune-dated :local: --keep-days 30 --keep-min 7", "root of :local:"),
        ("prune-dated :sftp,host=x,key_file=/k: --keep-days 1 --keep-min 1", "connection string"),
        ("prune-dated 'crypt,a=\"b:c\":' --keep-days 30 --keep-min 7", "connection string"),
        # Its options beat every flag, so copy_links there would follow symlinks.
        ("prune-dated :local,copy_links=true:/v --keep-days 1 --keep-min 1", "connection string"),
        ("prune-dated crypt,copy_links:v --keep-days 1 --keep-min 1", "connection string"),
        ("prune-dated crypt:versions/.. --keep-days 30 --keep-min 7", "must not contain .."),
        ("prune-dated crypt:versions --keep-min 7", "--keep-days N is required"),
        ("prune-dated crypt:versions --keep-days 30", "--keep-min N is required"),
        ("prune-dated crypt:versions --keep-days 30 --keep-min 0", "at least 1"),
        ("prune-dated crypt:versions --keep-days -1 --keep-min 7", "whole number"),
        ("prune-dated crypt:versions --keep-days 1.5 --keep-min 7", "whole number"),
        ("prune-dated crypt:versions --keep-days ३ --keep-min 7", "whole number"),
        ("prune-dated crypt:versions --keep-days 3 --keep-days 4 --keep-min 7", "more than once"),
        ("prune-dated crypt:versions --keep-days 3 --keep-min", "whole number"),
        ("prune-dated crypt:a crypt:b --keep-days 3 --keep-min 7", "one path, got 2"),
        # A flag's value given apart from it would be read as a second path.
        ("prune-dated crypt:a --config /x.conf --keep-days 3 --keep-min 7", "--flag=value"),
    ],
)
def test_refuses_unsafe_or_unclear_prune_steps(step, error):
    with pytest.raises(CommandPolicyError, match="^prune-dated: ") as caught:
        build_step_argv(step, "", {})

    assert error in str(caught.value)


def test_accepts_sub_folders_of_remotes_and_local_paths():
    for path in [
        "crypt:versions",
        "crypt:/a/versions/",
        ":local:/mnt/backup/versions",
        "/mnt/backup/versions",
        "/mnt/a:b",
        "/mnt/a,b",
        "versions",
    ]:
        assert check_path(path) == path


def test_a_stored_command_is_checked_again_before_running():
    for argv in [
        ["prune-dated", "crypt:", "--keep-days", "30", "--keep-min", "7"],
        ["prune-dated", "crypt:v", "--keep-days", "30", "--keep-min", "0"],
        ["prune-dated", "crypt:v", "--keep-min", "7", "--keep-days", "30"],
        ["prune-dated", "crypt:v"],
    ]:
        with pytest.raises(PruneCommandError):
            from_argv(argv)


def test_other_commands_still_go_to_rclone():
    assert build_step_argv("sync /a crypt:a", "--fast-list", {}) == [
        "rclone",
        "sync",
        "--fast-list",
        "/a",
        "crypt:a",
    ]
    # A prune step has no source to measure, even with a path named like a subcommand.
    argv = build_step_argv("prune-dated sync --keep-days 1 --keep-min 1", "", {})
    assert size_command(argv) is None


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], False),
        (["--dry-run"], True),
        (["-n"], True),
        (["-vn"], True),
        (["--dry-run", "--dry-run=false"], False),
        (["-n=false"], False),
        (["-vn=false"], False),
        (["--dry-run=true"], True),
        # Values of other flags, and long flags with an n in them, are not dry runs.
        (["--exclude", "Thumbnails", "--transfers=4", "--fast-list"], False),
        (["--bwlimit", "8M"], False),
    ],
)
def test_reads_dry_run_flags_as_rclone_does(flags, expected):
    assert dry_run_requested(flags) is expected


def test_filters_are_left_out_of_listing_and_purging():
    assert rclone_flags(
        [
            "--dry-run",
            "--max-depth",
            "1",
            "--copy-links",
            "-L",
            "-vL",
            "--exclude",
            "*.tmp",
            "--include=*.flac",
            "--files-from",
            "/list",
            "--min-age",
            "1d",
            "--ignore-case",
            "--error-on-no-transfer",
            "-f",
            "- x",
            "-vf",
            "- y",
            "-fz",
            "-vf=- w",
            # -f'- *.tmp' in the common arguments.
            "-f- *.tmp",
            "-f=- y",
            "--links=false",
            "--local-links",
            "-l=true",
            "-vl",
            "-Ln=false",
            "-L=true",
            "--transfers=4",
            "--bwlimit",
            "8M",
            "-v",
            "--max-size",
        ]
    ) == ["--dry-run", "-v", "-v", "-v", "-v", "-n=false", "--transfers=4", "--bwlimit", "8M", "-v"]


def test_filters_and_link_following_are_left_out_of_the_environment():
    env = {
        "RCLONE_EXCLUDE": "*.tmp",
        "RCLONE_FILES_FROM": "/list",
        "RCLONE_FILTER": "- x",
        "RCLONE_MAX_DEPTH": "1",
        "RCLONE_COPY_LINKS": "true",
        "RCLONE_LOCAL_COPY_LINKS": "true",
        "RCLONE_LINKS": "false",
        "RCLONE_LOCAL_LINKS": "false",
        "RCLONE_CONFIG": "/config/rclone.conf",
        "RCLONE_TRANSFERS": "4",
        "PATH": "/usr/bin",
    }

    assert rclone_environment(env) == {
        "RCLONE_CONFIG": "/config/rclone.conf",
        "RCLONE_TRANSFERS": "4",
        "PATH": "/usr/bin",
    }


def test_a_dropped_filter_value_still_counts_for_a_dry_run():
    argv = build_step_argv(
        "prune-dated crypt:v --keep-days 1 --keep-min 1", "--dry-run --exclude --dry-run", {}
    )

    assert from_argv(argv).dry_run
    assert "--dry-run" not in rclone_flags(from_argv(argv).rclone_flags)[1:]


def test_only_exact_valid_dates_count():
    assert dated_name("2026-11-16") == date(2026, 11, 16)
    for name in OTHERS + ["2026-11-16\n", "２０２６-11-16"]:
        assert dated_name(name) is None


def test_plan_purges_folders_older_than_keep_days_oldest_first():
    plan = plan_prune(DATED + OTHERS, date(2026, 11, 16), keep_days=30, keep_min=2)

    # 2026-10-17 is exactly 30 days old: not older than 30 days.
    assert plan.cutoff == date(2026, 10, 17)
    assert plan.purge == ["2026-01-05", "2026-06-30", "2026-10-01"]
    assert plan.keep == ["2026-11-16", "2026-11-01", "2026-10-17"]
    assert plan.ignored == sorted(OTHERS)


def test_plan_keeps_the_newest_folders_when_the_clock_is_far_ahead():
    plan = plan_prune(DATED, date(2999, 1, 1), keep_days=30, keep_min=2)

    assert plan.keep == ["2026-11-16", "2026-11-01"]
    assert plan.purge == DATED[:-2]


def test_plan_keeps_everything_when_the_clock_is_far_behind():
    plan = plan_prune(DATED, date(2000, 1, 1), keep_days=0, keep_min=1)

    assert plan.purge == []
    assert plan.keep == sorted(DATED, reverse=True)


def test_plan_with_fewer_folders_than_keep_min_purges_nothing():
    plan = plan_prune(["2001-01-01", "2002-01-01"], date(2026, 1, 1), keep_days=1, keep_min=5)

    assert plan.purge == []


def test_listing_strips_the_folder_slash():
    assert parse_listing("2026-01-05/\nnotes/\r\n 2026-01-06/\n2026-01-07\n/\n\n") == [
        "2026-01-05",
        "notes",
        " 2026-01-06",
    ]


# Against real rclone, with a local remote defined in the environment.


@pytest.fixture
def versions(tmp_path):
    root = tmp_path / "versions"
    for name in DATED + OTHERS:
        (root / name / "Music").mkdir(parents=True)
        (root / name / "Music" / "song.flac").write_bytes(b"x")
    # A file named like a date is not a folder.
    (root / "2020-01-01").write_bytes(b"x")
    return root


def _rclone_env(tmp_path) -> dict[str, str]:
    return os.environ | {
        "RCLONE_CONFIG": str(tmp_path / "rclone.conf"),
        "RCLONE_CONFIG_BACKUP_TYPE": "local",
    }


def _prune_argv(versions: Path, *flags: str, keep_days: int = 30, keep_min: int = 2):
    step = f"prune-dated backup:{versions} --keep-days {keep_days} --keep-min {keep_min}"
    return build_step_argv(step, " ".join(flags), {})


def _left(versions: Path) -> list[str]:
    return sorted(path.name for path in versions.iterdir())


@needs_rclone
async def test_real_prune_purges_old_dated_folders_and_logs_each(versions, tmp_path):
    log_path = tmp_path / "logs" / "prune.log"

    exit_code = await run_prune(
        _prune_argv(versions, "--verbose", "--error-on-no-transfer"),
        _rclone_env(tmp_path),
        log_path,
        date(2026, 11, 16),
        "Europe/Helsinki",
    )

    log = log_path.read_text()
    assert exit_code == 0, log
    assert _left(versions) == sorted(
        ["2026-10-17", "2026-11-01", "2026-11-16", "2020-01-01", *OTHERS]
    )
    for name in ["2026-01-05", "2026-06-30", "2026-10-01"]:
        assert f"[prune-dated] Purged backup:{versions}/{name}\n" in log
    assert "Today is 2026-11-16 (Europe/Helsinki): purging dated folders before 2026-10-17" in log
    assert "Leaving alone 6 other folder(s)" in log
    assert "Done: purged 3 folder(s), kept 3." in log


@needs_rclone
async def test_real_prune_ignores_the_syncs_filters(versions, tmp_path):
    log_path = tmp_path / "prune.log"
    (tmp_path / "list.txt").write_text("notes/x\n")

    exit_code = await run_prune(
        _prune_argv(
            versions,
            "--exclude '2026-0*/**' --files-from",
            str(tmp_path / "list.txt"),
            "--max-age 1d --ignore-case -f '- 2026-01-05/**' -vf'- 2026-06-30/**'",
        ),
        _rclone_env(tmp_path),
        log_path,
        date(2026, 11, 16),
        "UTC",
    )

    assert exit_code == 0, log_path.read_text()
    assert _left(versions) == sorted(
        ["2026-10-17", "2026-11-01", "2026-11-16", "2020-01-01", *OTHERS]
    )


@needs_rclone
async def test_real_prune_ignores_filters_and_depth_from_the_environment(versions, tmp_path):
    log_path = tmp_path / "prune.log"
    (tmp_path / "list.txt").write_text("notes/x\n")
    env = _rclone_env(tmp_path) | {
        "RCLONE_EXCLUDE": "2026-0*/**",
        "RCLONE_FILES_FROM": str(tmp_path / "list.txt"),
        "RCLONE_MAX_DEPTH": "1",
    }

    exit_code = await run_prune(
        _prune_argv(versions, "--max-depth=1"), env, log_path, date(2026, 11, 16), "UTC"
    )

    assert exit_code == 0, log_path.read_text()
    assert _left(versions) == sorted(
        ["2026-10-17", "2026-11-01", "2026-11-16", "2020-01-01", *OTHERS]
    )


@needs_rclone
async def test_real_prune_never_follows_symlinks(tmp_path):
    versions = tmp_path / "versions"
    outside = tmp_path / "outside"
    (versions / "2026-11-16" / "Music").mkdir(parents=True)
    (versions / "2026-11-16" / "Music" / "keep.flac").write_bytes(b"x")
    outside.mkdir()
    (outside / "precious").write_bytes(b"x")
    # An old folder with links to the kept folder and to elsewhere, and a dated link.
    (versions / "2020-01-01").mkdir()
    (versions / "2020-01-01" / "Music").symlink_to(versions / "2026-11-16" / "Music")
    (versions / "2020-01-01" / "out").symlink_to(outside)
    (versions / "2019-01-01").symlink_to(outside)
    log_path = tmp_path / "prune.log"
    # Every way of asking rclone to follow links: flags, environment, the remote's config.
    env = _rclone_env(tmp_path) | {
        "RCLONE_COPY_LINKS": "true",
        "RCLONE_CONFIG_BACKUP_COPY_LINKS": "true",
        "RCLONE_LINKS": "false",
        "RCLONE_CONFIG_BACKUP_LINKS": "false",
    }

    exit_code = await run_prune(
        _prune_argv(versions, "-L --copy-links --links=false -l=false", keep_min=1),
        env,
        log_path,
        date(2026, 11, 16),
        "UTC",
    )

    assert exit_code == 0, log_path.read_text()
    assert _left(versions) == ["2019-01-01", "2026-11-16"]
    assert (versions / "2026-11-16" / "Music" / "keep.flac").exists()
    assert (outside / "precious").exists()
    assert (versions / "2019-01-01").is_symlink()


@needs_rclone
async def test_real_prune_fails_rather_than_follow_links_a_remote_config_turns_on(tmp_path):
    # A connection string inside the remote's config beats every flag; rclone then refuses
    # the --links this step gives purge.
    versions = tmp_path / "versions"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"x")
    (versions / "2026-11-16").mkdir(parents=True)
    (versions / "2020-01-01").mkdir()
    (versions / "2020-01-01" / "out").symlink_to(outside)
    (versions / "2019-01-01").symlink_to(outside)
    env = _rclone_env(tmp_path) | {
        "RCLONE_CONFIG_LINKY_TYPE": "alias",
        "RCLONE_CONFIG_LINKY_REMOTE": f":local,copy_links=true:{tmp_path}",
    }
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        build_step_argv("prune-dated linky:versions --keep-days 1 --keep-min 1", "", {}),
        env,
        log_path,
        date(2026, 11, 16),
        "UTC",
    )

    assert exit_code != 0
    assert "can't use -l/--links with -L/--copy-links" in log_path.read_text()
    assert (outside / "precious").exists()


@needs_rclone
async def test_real_prune_reports_a_dry_run_from_the_environment(versions, tmp_path):
    before = _left(versions)
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        _prune_argv(versions),
        _rclone_env(tmp_path) | {"RCLONE_DRY_RUN": "true"},
        log_path,
        date(2999, 1, 1),
        "UTC",
    )

    assert exit_code == 0
    assert _left(versions) == before
    assert "Nothing was purged." in log_path.read_text()


@needs_rclone
async def test_real_prune_with_the_clock_far_ahead_keeps_the_newest(versions, tmp_path):
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        _prune_argv(versions, keep_min=2),
        _rclone_env(tmp_path),
        log_path,
        date(2999, 1, 1),
        "UTC",
    )

    assert exit_code == 0, log_path.read_text()
    assert _left(versions) == sorted(["2026-11-01", "2026-11-16", "2020-01-01", *OTHERS])
    # The other folders were left exactly as they were.
    assert (versions / "notes" / "Music" / "song.flac").exists()


@needs_rclone
async def test_real_prune_dry_run_only_lists(versions, tmp_path):
    before = _left(versions)
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        _prune_argv(versions, "--dry-run"),
        _rclone_env(tmp_path),
        log_path,
        date(2999, 1, 1),
        "UTC",
    )

    log = log_path.read_text()
    assert exit_code == 0, log
    assert _left(versions) == before
    assert f"Would purge backup:{versions}/2026-01-05" in log
    assert "Dry run: would purge 4 folder(s). Nothing was purged." in log
    assert "Purged" not in log


@needs_rclone
async def test_real_prune_of_a_folder_not_made_yet_succeeds(tmp_path):
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        _prune_argv(tmp_path / "missing"),
        _rclone_env(tmp_path),
        log_path,
        date(2026, 11, 16),
        "UTC",
    )

    assert exit_code == 0
    assert "doesn't exist yet. Nothing to prune." in log_path.read_text()


@needs_rclone
@pytest.mark.skipif(os.geteuid() == 0, reason="root can delete read-only files")
async def test_real_prune_stops_and_fails_when_a_purge_fails(versions, tmp_path):
    locked = versions / "2026-01-05" / "Music"
    locked.chmod(0o555)
    log_path = tmp_path / "prune.log"
    try:
        exit_code = await run_prune(
            _prune_argv(versions, "--retries=1"),
            _rclone_env(tmp_path),
            log_path,
            date(2026, 11, 16),
            "UTC",
        )
    finally:
        locked.chmod(0o755)

    log = log_path.read_text()
    assert exit_code != 0
    assert f"Purging backup:{versions}/2026-01-05 failed with exit code {exit_code}" in log
    assert "Stopped after purging 0 of 3 folder(s)." in log
    # It stopped at the first failure, oldest first.
    assert {"2026-01-05", "2026-06-30", "2026-10-01"} <= set(_left(versions))


@needs_rclone
async def test_real_prune_fails_when_listing_fails(tmp_path):
    log_path = tmp_path / "prune.log"

    exit_code = await run_prune(
        build_step_argv("prune-dated nowhere:versions --keep-days 1 --keep-min 1", "", {}),
        _rclone_env(tmp_path),
        log_path,
        date(2026, 11, 16),
        "UTC",
    )

    assert exit_code != 0
    assert "Listing failed with exit code" in log_path.read_text()


# Through the job runners.


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _prune_job(versions: Path, tmp_path: Path) -> Job:
    return Job(
        id=3,
        name="backup",
        # The syncs' filters must not stop the purge, which refuses them.
        common_args="--verbose --exclude '*.tmp' --min-age=1d -f '- x' --ignore-case",
        env={
            "RCLONE_CONFIG": str(tmp_path / "rclone.conf"),
            "RCLONE_CONFIG_BACKUP_TYPE": "local",
        },
        steps=[
            JobStep(id=1, name="Sync", command=f"lsd backup:{versions}", position=1),
            JobStep(
                id=2,
                name="Prune",
                command=f"prune-dated backup:{versions} --keep-days 30 --keep-min 2",
                position=2,
            ),
        ],
    )


@needs_rclone
@pytest.mark.parametrize("dry_run", [False, True])
async def test_live_runner_prunes_as_the_last_step_with_the_clock_far_ahead(
    versions, tmp_path, monkeypatch, dry_run
):
    monkeypatch.setattr(runner_module, "utc_now", lambda: datetime(2999, 1, 1, 3, tzinfo=UTC))
    before = _left(versions)
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    first = runner.start_job(_prune_job(versions, tmp_path), trigger="manual", dry_run=dry_run)
    await runner.wait_for_job_run(first.job_run_id)

    with session_factory() as db:
        steps = db.query(JobStepRunRecord).order_by(JobStepRunRecord.id).all()
        assert [step.status for step in steps] == ["success", "success"]
        prune_step = steps[1]
        assert json.loads(prune_step.argv_json)[:2] == ["prune-dated", f"backup:{versions}"]
        log = Path(prune_step.log_path).read_text()
    assert "Today is 2999-01-01" in log
    if dry_run:
        assert _left(versions) == before
        assert "Nothing was purged." in log
    else:
        assert _left(versions) == sorted(["2026-11-01", "2026-11-16", "2020-01-01", *OTHERS])


async def test_live_runner_does_not_prune_after_a_failed_step(tmp_path):
    pruned = []

    async def executor(argv, env, log_path):
        return 1

    async def prune(argv, env, log_path, today):
        pruned.append(argv)
        return 0

    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(
        tmp_path / "logs", session_factory=session_factory, executor=executor, prune=prune
    )

    first = runner.start_job(_prune_job(tmp_path / "versions", tmp_path), trigger="schedule")
    await runner.wait_for_job_run(first.job_run_id)

    assert pruned == []


async def test_runners_count_prune_days_from_the_run_date_in_the_app_time_zone(
    tmp_path, monkeypatch
):
    # 22:30 UTC on 31 December is already 1 January in Helsinki.
    monkeypatch.setattr(runner_module.settings, "timezone", "Europe/Helsinki")
    monkeypatch.setattr(
        runner_module, "utc_now", lambda: datetime(2026, 12, 31, 22, 30, tzinfo=UTC)
    )
    calls = []

    async def executor(argv, env, log_path):
        calls.append(("rclone", argv))
        return 0

    async def prune(argv, env, log_path, today):
        calls.append(("prune", argv[:2], env, today))
        return 0

    job = _prune_job(Path("/v"), tmp_path)
    result = await JobRunner(tmp_path / "simple", executor=executor, prune=prune).run_job(
        job, trigger="manual", step_id=2
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(
        tmp_path / "logs", session_factory=session_factory, executor=executor, prune=prune
    )
    first = runner.start_job(job, trigger="manual", step_id=2)
    await runner.wait_for_job_run(first.job_run_id)

    assert result.success
    expected = ("prune", ["prune-dated", "backup:/v"], job.env, date(2027, 1, 1))
    assert calls == [expected, expected]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["rclone", "sync", "--dry-run", "a", "b"], False),
        (["rclone", "sync", "--dry-run=true", "-n=1", "-vn", "a", "b"], False),
        (["rclone", "sync", "--dry-run", "a", "b", "--dry-run=false"], True),
        (["rclone", "sync", "a", "b", "-n=false"], True),
        (["rclone", "sync", "a", "b", "-vn=0"], True),
        # rclone rejects this value; refusing is the safe side.
        (["rclone", "sync", "a", "b", "--dry-run=no"], True),
        # In "-nv=false" the false belongs to -v.
        (["rclone", "sync", "a", "b", "-nv=false"], False),
        (["prune-dated", "c:v", "--keep-days", "1", "--keep-min", "1", "--dry-run=f"], True),
    ],
)
def test_finds_arguments_that_could_turn_a_dry_run_off(args, expected):
    assert may_turn_off_dry_run(args) is expected


@pytest.mark.parametrize(
    "command",
    [
        "sync /a crypt:a --dry-run=false",
        "prune-dated crypt:versions --keep-days 30 --keep-min 2 --dry-run=false",
    ],
)
async def test_a_requested_dry_run_refuses_steps_that_turn_dry_runs_off(tmp_path, command):
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    async def prune(argv, env, log_path, today):
        calls.append(argv)
        return 0

    job = Job(
        id=4,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="Safe", command="sync /b crypt:b", position=1),
            JobStep(id=2, name="Unsafe", command=command, position=2),
        ],
    )
    with pytest.raises(CommandPolicyError, match="turn dry runs off"):
        await JobRunner(tmp_path / "simple", executor=executor, prune=prune).run_job(
            job, trigger="manual-dry-run", dry_run=True
        )
    # It checks each step as it reaches it: the safe one ran, as a dry run.
    assert calls == [["rclone", "sync", "--dry-run", "/b", "crypt:b"]]
    calls.clear()
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(
        tmp_path / "logs", session_factory=session_factory, executor=executor, prune=prune
    )
    refused = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
    # A real run of the same job is still allowed.
    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)

    assert refused.status == "failed"
    assert "turn dry runs off" in Path(refused.log_path).read_text()
    # Nothing ran for the dry run, which checks every step first; the real run ran both.
    assert len(calls) == 2
    assert calls[0] == ["rclone", "sync", "/b", "crypt:b"]
    assert "--dry-run=false" in calls[1]


# Cancelling a prune step, with a fake rclone that ignores SIGTERM.

FAKE_RCLONE = """
import os, signal, sys, time
command = sys.argv[1]
state = os.environ["FAKE_RCLONE_STATE"]
if command == "lsf" and os.environ["FAKE_RCLONE_HANG"] != "lsf":
    print("2020-01-01/\\n2020-01-02/\\n2026-11-16/")
    sys.exit(0)
if command == "purge":
    with open(os.path.join(state, "purges"), "a") as purges:
        purges.write(sys.argv[-1] + "\\n")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.path.join(state, "pid.tmp"), "w") as pid_file:
    pid_file.write(str(os.getpid()))
os.replace(os.path.join(state, "pid.tmp"), os.path.join(state, "pid"))
while True:
    time.sleep(1)
"""


@pytest.mark.parametrize("hang", ["lsf", "purge"])
async def test_cancelling_a_prune_stops_rclone_and_purges_nothing_more(tmp_path, monkeypatch, hang):
    monkeypatch.setattr(prune_module, "TERMINATE_TIMEOUT_SECONDS", 0.5)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "rclone"
    fake.write_text(f"#!{sys.executable}\n{FAKE_RCLONE}")
    fake.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_RCLONE_STATE": str(tmp_path),
        "FAKE_RCLONE_HANG": hang,
    }
    argv = build_step_argv("prune-dated backup:versions --keep-days 30 --keep-min 1", "", {})
    task = asyncio.create_task(
        run_prune(argv, env, tmp_path / "prune.log", date(2026, 11, 16), "UTC")
    )
    pid_path = tmp_path / "pid"
    for _ in range(200):
        if pid_path.exists():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_path.read_text())

    # A second cancel (a repeated Cancel click) must not interrupt the shutdown.
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, signal.SIGKILL)
        raise AssertionError(f"rclone {pid} was still running")
    purges = tmp_path / "purges"
    if hang == "lsf":
        assert not purges.exists()
    else:
        assert purges.read_text() == "backup:versions/2020-01-01\n"

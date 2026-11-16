import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.runner as runner_module
from app.core.commands import DRY_RUN_SAFEGUARDS, CommandPolicyError
from app.core.models import Job, JobStep, utc_now
from app.core.runner import STATS_FLAGS, JobRunner, LiveJobRunner, subprocess_executor
from app.db import Base, ConsoleRunRecord, JobRunRecord, JobStepRunRecord
from app.main import run_mode_label


@pytest.mark.asyncio
async def test_stops_remaining_steps_after_failure():
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 7 if len(calls) == 1 else 0

    job = Job(
        id=1,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="first", command="sync /source secret:/source"),
            JobStep(id=2, name="second", command="sync /other secret:/other"),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = await JobRunner(Path(tmpdir), executor=executor).run_job(job, trigger="manual")

    assert not result.success
    assert result.exit_code == 7
    assert len(result.step_runs) == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_skips_overlapping_run_of_same_job():
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        started.set()
        await release.wait()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = JobRunner(Path(tmpdir), executor=executor)
        first = asyncio.create_task(runner.run_job(job, trigger="schedule"))
        await started.wait()
        second = await runner.run_job(job, trigger="schedule")
        release.set()
        first_result = await first

    assert second.status == "skipped"
    assert first_result.success


@pytest.mark.asyncio
async def test_dry_run_injects_dry_run_arg_after_rclone_operation():
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    job = Job(
        id=7,
        name="backup",
        common_args="--fast-list",
        env={},
        steps=[JobStep(id=1, name="one", command="sync /source secret:/source")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = await JobRunner(Path(tmpdir), executor=executor).run_job(
            job, trigger="manual-dry-run", dry_run=True
        )

    assert result.success
    assert calls == [
        [
            "rclone",
            "sync",
            *STATS_FLAGS,
            *DRY_RUN_SAFEGUARDS,
            "--dry-run",
            "--fast-list",
            "/source",
            "secret:/source",
        ]
    ]


@pytest.mark.asyncio
async def test_dry_run_comes_first_even_when_the_common_args_name_it():
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    # Here --dry-run is the pattern to exclude, not a flag.
    job = Job(
        id=8,
        name="backup",
        common_args="--exclude --dry-run",
        env={},
        steps=[JobStep(id=1, name="one", command="sync /source secret:/source")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        await JobRunner(Path(tmpdir), executor=executor).run_job(
            job, trigger="manual-dry-run", dry_run=True
        )

    assert calls == [
        [
            "rclone",
            "sync",
            *STATS_FLAGS,
            *DRY_RUN_SAFEGUARDS,
            "--dry-run",
            "--exclude",
            "--dry-run",
            "/source",
            "secret:/source",
        ]
    ]


@pytest.mark.asyncio
async def test_can_run_one_selected_step():
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    job = Job(
        id=9,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="first", command="sync /first secret:/first"),
            JobStep(id=2, name="second", command="sync /second secret:/second"),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = await JobRunner(Path(tmpdir), executor=executor).run_job(
            job, trigger="manual-step", step_id=2
        )

    assert result.success
    assert [step.step_name for step in result.step_runs] == ["second"]
    assert calls == [["rclone", "sync", *STATS_FLAGS, "/second", "secret:/second"]]


def test_run_mode_labels_distinguish_dry_runs():
    assert run_mode_label("manual") == "Run"
    assert run_mode_label("manual-step") == "Step run"
    assert run_mode_label("schedule") == "Scheduled"
    assert run_mode_label("manual-dry-run") == "Dry run"
    assert run_mode_label("manual-step-dry-run") == "Dry run"


@pytest.mark.asyncio
async def test_live_runner_creates_step_record_before_executor_finishes():
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n", encoding="utf-8")
        started.set()
        await release.wait()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")

        assert first_step is not None
        assert first_step.status == "running"
        assert first_step.exit_code is None
        assert first_step.ended_at is None

        await started.wait()
        release.set()
        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            finished = db.get(JobStepRunRecord, first_step.id)
            assert finished is not None
            assert finished.status == "success"
            assert finished.exit_code == 0
            assert finished.ended_at is not None


@pytest.mark.asyncio
async def test_live_runner_stores_parsed_transfer_stats_when_step_finishes():
    async def executor(argv, env, log_path):
        log_path.write_text(
            "\n".join(
                [
                    "Transferred:      1.500 MiB / 1.500 MiB, 100%",
                    "Deleted:              2 (files), 1 (dirs), 4 KiB (freed)",
                    "Transferred:          7 / 7, 100%",
                ]
            ),
            encoding="utf-8",
        )
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="sync /src remote:dst")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        assert first_step is not None

        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            finished = db.get(JobStepRunRecord, first_step.id)
            assert finished.transfer_stats_json == (
                '{"deleted_dirs": 1, "deleted_files": 2, "renamed_files": 0, '
                '"transferred_bytes": 1572864, "transferred_files": 7}'
            )


@pytest.mark.asyncio
async def test_live_runner_start_job_works_with_expiring_sessions():
    started = asyncio.Event()

    async def executor(argv, env, log_path):
        started.set()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db", expire_on_commit=True)
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)

        first_step = runner.start_job(job, trigger="manual-step-dry-run", dry_run=True, step_id=1)

        assert first_step is not None
        assert first_step.job_run_id == 1
        await started.wait()
        await runner.wait_for_job_run(first_step.job_run_id)


@pytest.mark.asyncio
async def test_live_runner_cancel_marks_job_canceled_and_skips_remaining_steps():
    started = asyncio.Event()
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        started.set()
        await asyncio.sleep(30)
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="one", command="lsd secret:"),
            JobStep(id=2, name="two", command="lsd secret:/two"),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        assert first_step is not None

        await started.wait()
        assert runner.cancel_job_run(first_step.job_run_id)
        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            step_runs = db.query(JobStepRunRecord).filter_by(job_run_id=job_run.id).all()
            assert job_run.status == "canceled"
            assert job_run.ended_at is not None
            assert [step.status for step in step_runs] == ["canceled"]
            assert step_runs[0].exit_code is None
            assert len(calls) == 1


@pytest.mark.asyncio
async def test_live_runner_marks_executor_exception_as_failed():
    async def executor(argv, env, log_path):
        raise FileNotFoundError("rclone")

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        assert first_step is not None

        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            step_run = db.get(JobStepRunRecord, first_step.id)
            assert job_run.status == "failed"
            assert job_run.ended_at is not None
            assert step_run.status == "failed"
            assert step_run.exit_code is None
            assert step_run.ended_at is not None


@pytest.mark.asyncio
async def test_live_runner_notifies_after_terminal_job_status():
    notified = []

    async def executor(argv, env, log_path):
        return 0

    async def notification_callback(job_run_id):
        notified.append(job_run_id)

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=session_factory,
            executor=executor,
            notification_callback=notification_callback,
        )
        first_step = runner.start_job(job, trigger="manual")
        assert first_step is not None

        await runner.wait_for_job_run(first_step.job_run_id)
        await runner.wait_for_notifications()

        with session_factory() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            assert job_run.status == "success"
        assert notified == [first_step.job_run_id]


@pytest.mark.asyncio
async def test_live_runner_frees_the_job_before_its_email_is_sent():
    # A slow mail server must not make the job's next run (say, the next scheduled backup)
    # look like it overlaps the one that has finished.
    sending = asyncio.Event()
    release = asyncio.Event()
    notified = []

    async def executor(argv, env, log_path):
        return 0

    async def notification_callback(job_run_id):
        sending.set()
        await release.wait()
        notified.append(job_run_id)

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=session_factory,
            executor=executor,
            notification_callback=notification_callback,
        )
        first = runner.start_job(job, trigger="schedule")
        assert first is not None
        await asyncio.wait_for(sending.wait(), 5)
        with session_factory() as db:
            assert db.get(JobRunRecord, first.job_run_id).status == "success"

        second = runner.start_job(job, trigger="schedule")

        assert second is not None
        assert second.status == "running"
        await runner.wait_for_job_run(second.job_run_id)
        with session_factory() as db:
            assert db.query(JobRunRecord).filter_by(status="skipped").count() == 0
            assert db.get(JobRunRecord, second.job_run_id).status == "success"
        release.set()
        await runner.wait_for_notifications()
        assert sorted(notified) == [first.job_run_id, second.job_run_id]


@pytest.mark.asyncio
async def test_live_runner_end_of_an_earlier_run_does_not_free_a_later_one():
    # The earlier run's task finishing must not mark the job idle while its next run goes on.
    finish_second = asyncio.Event()
    calls = 0

    async def executor(argv, env, log_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            await finish_second.wait()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first = runner.start_job(job, trigger="manual")
        while runner._active_runs:
            await asyncio.sleep(0)
        # The first run has freed the job, but its task may not have finished yet.
        second = runner.start_job(job, trigger="manual")
        assert second is not None
        await runner.wait_for_job_run(first.job_run_id)
        for _ in range(5):
            await asyncio.sleep(0)

        assert runner.start_job(job, trigger="manual") is None
        finish_second.set()
        await runner.wait_for_job_run(second.job_run_id)
        third = runner.start_job(job, trigger="manual")
        assert third is not None
        await runner.wait_for_job_run(third.job_run_id)
        with session_factory() as db:
            assert db.get(JobRunRecord, third.job_run_id).status == "success"


@pytest.mark.asyncio
async def test_live_runner_notification_failure_does_not_change_job_status():
    async def executor(argv, env, log_path):
        return 0

    async def notification_callback(job_run_id):
        raise RuntimeError("smtp unavailable")

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=session_factory,
            executor=executor,
            notification_callback=notification_callback,
        )
        first_step = runner.start_job(job, trigger="manual")
        assert first_step is not None

        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            assert job_run.status == "success"


async def _drain_notifications():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_live_runner_records_invalid_command_and_does_not_block_later_runs():
    calls = []
    notified = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    async def notification_callback(job_run_id):
        notified.append(job_run_id)

    job = Job(
        id=42,
        name="backup",
        common_args="--bwlimit ${BW_LIMIT}",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=session_factory,
            executor=executor,
            notification_callback=notification_callback,
        )

        failed_step = runner.start_job(job, trigger="schedule")
        await _drain_notifications()

        assert failed_step is not None
        assert failed_step.status == "failed"
        assert "missing environment variable: BW_LIMIT" in Path(failed_step.log_path).read_text()
        with session_factory() as db:
            job_run = db.get(JobRunRecord, failed_step.job_run_id)
            assert job_run.status == "failed"
            assert job_run.ended_at is not None
        assert notified == [failed_step.job_run_id]
        assert calls == []

        job.env = {"BW_LIMIT": "8M"}
        next_step = runner.start_job(job, trigger="schedule")

        assert next_step is not None
        assert next_step.status == "running"
        await runner.wait_for_job_run(next_step.job_run_id)
        assert calls == [["rclone", "lsd", "--bwlimit", "8M", "secret:"]]


@pytest.mark.asyncio
async def test_live_runner_checks_every_step_before_running_any():
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="first", command="lsd secret:", position=1),
            JobStep(id=2, name="second", command="lsd ${MISSING}:", position=2),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)

        failed_step = runner.start_job(job, trigger="manual")

        assert failed_step is not None
        assert failed_step.step_name == "second"
        assert failed_step.status == "failed"
        assert calls == []


@pytest.mark.asyncio
async def test_live_runner_writes_unexpected_executor_errors_to_step_log():
    async def executor(argv, env, log_path):
        raise FileNotFoundError("rclone")

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        await runner.wait_for_job_run(first_step.job_run_id)

        assert "FileNotFoundError('rclone')" in Path(first_step.log_path).read_text()


@pytest.mark.asyncio
async def test_recover_interrupted_runs_closes_stale_runs_and_notifies():
    notified = []

    async def notification_callback(job_run_id):
        notified.append(job_run_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "step.log"
        log_path.write_text("Transferred: 1 / 10\n", encoding="utf-8")
        with session_factory() as db:
            job_run = JobRunRecord(
                job_id=1,
                job_name="backup",
                trigger="schedule",
                status="running",
                started_at=utc_now(),
                ended_at=None,
            )
            finished_run = JobRunRecord(
                job_id=1,
                job_name="backup",
                trigger="schedule",
                status="success",
                started_at=utc_now(),
                ended_at=utc_now(),
            )
            db.add_all([job_run, finished_run])
            db.flush()
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=1,
                    step_name="one",
                    argv_json="[]",
                    status="running",
                    exit_code=None,
                    log_path=str(log_path),
                    started_at=job_run.started_at,
                    ended_at=None,
                )
            )
            db.add(
                ConsoleRunRecord(
                    status="running",
                    command="config",
                    argv_json="[]",
                    exit_code=None,
                    log_path=str(Path(tmpdir) / "console.log"),
                    started_at=utc_now(),
                    ended_at=None,
                )
            )
            db.commit()
            stale_id = job_run.id
            finished_id = finished_run.id

        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=session_factory,
            notification_callback=notification_callback,
        )
        recovered = runner.recover_interrupted_runs()
        await _drain_notifications()

        assert recovered == [stale_id]
        assert notified == [stale_id]
        with session_factory() as db:
            stale = db.get(JobRunRecord, stale_id)
            assert stale.status == "failed"
            assert stale.ended_at is not None
            assert stale.step_runs[0].status == "failed"
            assert stale.step_runs[0].ended_at is not None
            assert db.get(JobRunRecord, finished_id).status == "success"
            assert db.query(ConsoleRunRecord).one().status == "canceled"
        assert "app restarted" in log_path.read_text()


@pytest.mark.asyncio
async def test_recover_interrupted_runs_still_commits_when_log_is_unwritable():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        unwritable_log = Path(tmpdir) / "log-is-a-directory"
        unwritable_log.mkdir()
        with session_factory() as db:
            job_run = JobRunRecord(
                job_id=1,
                job_name="backup",
                trigger="schedule",
                status="running",
                started_at=utc_now(),
                ended_at=None,
            )
            db.add(job_run)
            db.flush()
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=1,
                    step_name="one",
                    argv_json="[]",
                    status="running",
                    exit_code=None,
                    log_path=str(unwritable_log),
                    started_at=job_run.started_at,
                    ended_at=None,
                )
            )
            db.commit()
            job_run_id = job_run.id

        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory)

        assert runner.recover_interrupted_runs() == [job_run_id]
        with session_factory() as db:
            recovered = db.get(JobRunRecord, job_run_id)
            assert recovered.status == "failed"
            assert recovered.step_runs[0].status == "failed"


@pytest.mark.asyncio
async def test_live_runner_records_invalid_command_when_log_is_unwritable():
    job = Job(
        id=42,
        name="backup",
        common_args="${MISSING}",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_root = Path(tmpdir) / "logs"
        log_root.mkdir()
        (log_root / "job-42").write_text("a file where the log directory should be")
        runner = LiveJobRunner(log_root, session_factory=session_factory)

        failed_step = runner.start_job(job, trigger="schedule")

        assert failed_step is not None
        assert failed_step.status == "failed"
        with session_factory() as db:
            assert db.get(JobRunRecord, failed_step.job_run_id).status == "failed"


@pytest.mark.asyncio
async def test_subprocess_executor_logs_output_lines_longer_than_stream_limit(tmp_path: Path):
    log_path = tmp_path / "step.log"
    script = "import sys; sys.stdout.write('x' * 200_000); sys.stdout.flush()"

    exit_code = await subprocess_executor([sys.executable, "-c", script], {}, log_path)

    assert exit_code == 0
    assert log_path.read_bytes() == b"x" * 200_000


@pytest.mark.asyncio
async def test_subprocess_executor_stops_process_when_log_write_fails(tmp_path: Path):
    if not Path("/dev/full").exists():
        pytest.skip("needs /dev/full to simulate a full disk")
    pid_path = tmp_path / "child.pid"
    script = (
        "import os, sys, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    sys.stdout.write('progress\\n'); sys.stdout.flush(); time.sleep(0.05)\n"
    )

    with pytest.raises(OSError):
        await asyncio.wait_for(
            subprocess_executor([sys.executable, "-c", script], {}, Path("/dev/full")),
            timeout=10,
        )

    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_live_runner_stores_marker_when_step_log_has_no_stats():
    async def executor(argv, env, log_path):
        log_path.write_text("ERROR : nothing useful\n", encoding="utf-8")
        return 1

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        await runner.wait_for_job_run(first_step.job_run_id)

        with session_factory() as db:
            assert db.get(JobStepRunRecord, first_step.id).transfer_stats_json == "null"


@pytest.mark.asyncio
async def test_subprocess_executor_hides_app_settings_from_rclone(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RCLONE_RUNNER_SECRET_KEY", "session-secret")
    log_path = tmp_path / "step.log"
    names = "{'RCLONE_RUNNER_SECRET_KEY', 'BW_LIMIT'}"
    script = f"import os; print(sorted(k for k in os.environ if k in {names}))"

    await subprocess_executor([sys.executable, "-c", script], {"BW_LIMIT": "8M"}, log_path)

    assert log_path.read_text().strip() == "['BW_LIMIT']"


SIGTERM_IGNORING_CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


async def _wait_for_pid(pid_path: Path) -> int:
    for _ in range(200):
        if pid_path.exists() and pid_path.read_text():
            return int(pid_path.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("child did not start")


def _assert_reaped(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"process {pid} was still running")


@pytest.mark.asyncio
async def test_subprocess_executor_finishes_shutdown_when_cancelled_twice(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(runner_module, "TERMINATE_TIMEOUT_SECONDS", 0.5)
    pid_path = tmp_path / "child.pid"
    task = asyncio.create_task(
        subprocess_executor(
            [sys.executable, "-c", SIGTERM_IGNORING_CHILD, str(pid_path)], {}, tmp_path / "log"
        )
    )
    pid = await _wait_for_pid(pid_path)

    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    _assert_reaped(pid)


@pytest.mark.asyncio
async def test_cancel_job_run_cancels_the_task_only_once():
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        await release.wait()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        task = runner._tasks[first_step.job_run_id]

        assert runner.cancel_job_run(first_step.job_run_id) is True
        assert runner.cancel_job_run(first_step.job_run_id) is True
        assert task.cancelling() == 1

        await runner.wait_for_job_run(first_step.job_run_id)


def _session_factory(database_path: Path, expire_on_commit: bool = False):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=expire_on_commit
    )


@pytest.mark.asyncio
async def test_live_runner_gives_every_step_the_run_start_date(monkeypatch):
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        return 0

    started = datetime(2026, 11, 14, 21, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(runner_module, "utc_now", lambda: started)
    monkeypatch.setattr(runner_module.settings, "timezone", "Europe/Helsinki")
    job = Job(
        id=7,
        name="backup",
        common_args="--backup-dir crypt:versions/${RUN_DATE}",
        env={"RUN_TIME": "custom"},
        steps=[
            JobStep(id=1, name="one", command="sync /a crypt:current/a"),
            JobStep(id=2, name="two", command="sync /b crypt:current/b-${RUN_TIME}"),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = LiveJobRunner(
            Path(tmpdir),
            session_factory=_session_factory(Path(tmpdir) / "runs.db"),
            executor=executor,
        )
        first = runner.start_job(job, trigger="manual")
        await runner.wait_for_job_run(first.job_run_id)

    assert [argv[argv.index("--backup-dir") + 1] for argv in calls] == [
        "crypt:versions/2026-11-14",
        "crypt:versions/2026-11-14",
    ]
    # A job's own variable with the same name wins.
    assert calls[1][-1] == "crypt:current/b-custom"


@pytest.mark.asyncio
async def test_live_runs_started_in_the_same_second_keep_separate_logs(monkeypatch):
    from app.main import _delete_job_run

    runs = 0

    async def executor(argv, env, log_path):
        with log_path.open("wb") as log_file:
            log_file.write(f"run {runs}\n".encode())
        return 0

    started = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(runner_module, "utc_now", lambda: started)
    job = Job(
        id=1,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        steps = []
        for _ in range(2):
            runs += 1
            step = runner.start_job(job, trigger="manual")
            assert step is not None
            await runner.wait_for_job_run(step.job_run_id)
            steps.append(step)
            # Let the runner mark the job as no longer running.
            await asyncio.sleep(0)

        first, second = (Path(step.log_path) for step in steps)
        assert first != second
        assert first.read_text() == "run 1\n"
        assert second.read_text() == "run 2\n"

        with session_factory() as db:
            _delete_job_run(db, db.get(JobRunRecord, steps[0].job_run_id))
            db.commit()
        assert not first.exists()
        assert second.read_text() == "run 2\n"


@pytest.mark.asyncio
async def test_simple_runs_started_in_the_same_second_keep_separate_logs(monkeypatch):
    async def executor(argv, env, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("output", encoding="utf-8")
        return 0

    started = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(runner_module, "utc_now", lambda: started)
    job = Job(
        id=1,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = JobRunner(Path(tmpdir), executor=executor)
        first = await runner.run_job(job, trigger="manual")
        second = await runner.run_job(job, trigger="manual")

    assert first.step_runs[0].log_path != second.step_runs[0].log_path


@pytest.mark.asyncio
async def test_transfer_steps_ask_rclone_for_its_totals_without_storing_it(tmp_path):
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    job = Job(
        id=1,
        name="backup",
        common_args="--fast-list",
        env={},
        steps=[
            JobStep(id=1, name="copy", command="copy /a r:a --stats-log-level INFO", position=1),
            JobStep(id=2, name="list", command="lsf r:a", position=2),
        ],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory, executor=executor)
    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)
    result = await JobRunner(tmp_path / "simple", executor=executor).run_job(job, trigger="manual")

    copy = ["rclone", "copy", "--fast-list", "/a", "r:a", "--stats-log-level", "INFO"]
    listing = ["rclone", "lsf", "--fast-list", "r:a"]
    # Right after the subcommand, so the job's own --stats-log-level comes later and wins.
    assert calls == [[*copy[:2], *STATS_FLAGS, *copy[2:]], listing] * 2
    # The stored command stays the job's own, to copy and rerun as is.
    with session_factory() as db:
        stored = [json.loads(run.argv_json) for run in db.query(JobStepRunRecord).all()]
    assert stored == [copy, listing]
    assert [step_run.argv for step_run in result.step_runs] == [copy, listing]

    # One set in the environment would lose to the flag, so then it is left out.
    calls.clear()
    job.env = {"RCLONE_STATS_LOG_LEVEL": "DEBUG"}
    await JobRunner(tmp_path / "simple", executor=executor).run_job(job, trigger="manual")
    assert calls[0] == [*copy[:2], "--stats=1m", *copy[2:]]
    calls.clear()
    job.env = {"RCLONE_STATS_LOG_LEVEL": "DEBUG", "RCLONE_STATS": "10s"}
    await JobRunner(tmp_path / "simple", executor=executor).run_job(job, trigger="manual")
    assert calls[0] == copy


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
@pytest.mark.parametrize(("command", "deleted"), [("delete", 2), ("purge", 2), ("deletefile", 1)])
async def test_deleting_steps_report_their_totals_without_verbose_logging(
    tmp_path, command, deleted
):
    # rclone's delete, deletefile and purge log no totals at all without --stats.
    target = tmp_path / "old"
    (target / "sub").mkdir(parents=True)
    (target / "a.txt").write_text("a", encoding="utf-8")
    (target / "sub" / "b.txt").write_text("b", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.touch()
    path = target / "a.txt" if command == "deletefile" else target
    job = Job(
        id=1,
        name="tidy",
        common_args=f"--config {config}",
        env={},
        steps=[JobStep(id=1, name=command, command=f"{command} {path}")],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)

    with session_factory() as db:
        step_run = db.get(JobStepRunRecord, first.id)
        assert step_run.status == "success"
        stored = json.loads(step_run.transfer_stats_json)
        assert (stored["deleted_files"], stored["transferred_bytes"]) == (deleted, 0)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_a_copy_without_verbose_logging_reports_its_totals(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    config = tmp_path / "rclone.conf"
    config.touch()
    job = Job(
        id=1,
        name="backup",
        common_args=f"--config {config}",
        env={},
        steps=[JobStep(id=1, name="copy", command=f"copy {source} {tmp_path / 'dst'}")],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)

    with session_factory() as db:
        step_run = db.get(JobStepRunRecord, first.id)
        assert step_run.status == "success"
        stored = json.loads(step_run.transfer_stats_json)
        assert (stored["transferred_files"], stored["transferred_bytes"]) == (2, 2)


def _rename_case(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    (tmp_path / "src" / "new").write_text("abc", encoding="utf-8")
    (tmp_path / "dst" / "old").write_text("abc", encoding="utf-8")

    def changed():
        return sorted(path.name for path in (tmp_path / "dst").iterdir()) == ["new"]

    return f"sync {tmp_path / 'src'} {tmp_path / 'dst'} --track-renames", changed


def _empty_folders_case(tmp_path):
    (tmp_path / "tree" / "sub").mkdir(parents=True)
    return f"purge {tmp_path / 'tree'}", lambda: not (tmp_path / "tree").exists()


def _create_folders_case(tmp_path):
    (tmp_path / "src" / "empty").mkdir(parents=True)
    command = f"copy {tmp_path / 'src'} {tmp_path / 'dst'} --create-empty-src-dirs"
    return command, lambda: (tmp_path / "dst" / "empty").is_dir()


def _modtime_case(tmp_path):
    import os

    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    (tmp_path / "src" / "a").write_text("abc", encoding="utf-8")
    (tmp_path / "dst" / "a").write_text("abc", encoding="utf-8")
    os.utime(tmp_path / "dst" / "a", (1_000_000_000, 1_000_000_000))

    def changed():
        source, destination = (tmp_path / side / "a" for side in ("src", "dst"))
        return abs(destination.stat().st_mtime - source.stat().st_mtime) < 1

    return f"sync {tmp_path / 'src'} {tmp_path / 'dst'}", changed


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
@pytest.mark.parametrize(
    ("case", "label"),
    [
        (_rename_case, "0 B, 0 files, 0 deleted, 1 renamed"),
        (_empty_folders_case, "0 B, 0 files, 0 deleted, 2 folders removed"),
        # rclone counts neither a folder made nor a modification time updated: its totals
        # tell only that nothing was transferred, not that nothing changed.
        (_create_folders_case, "Nothing transferred"),
        (_modtime_case, "Nothing transferred"),
    ],
)
async def test_changes_that_transfer_nothing_are_not_called_no_changes(tmp_path, case, label):
    from app.core.rclone_stats import step_stats_display, step_transfer_stats

    command, changed = case(tmp_path)
    config = tmp_path / "rclone.conf"
    config.touch()
    job = Job(
        id=1,
        name="job",
        common_args=f"--config {config}",
        env={},
        steps=[JobStep(id=1, name="step", command=command)],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    first = runner.start_job(job, trigger="manual")
    await runner.wait_for_job_run(first.job_run_id)

    assert changed()  # rclone did change something...
    with session_factory() as db:
        step_run = db.get(JobStepRunRecord, first.id)
        assert step_run.status == "success"
        # ...which the label doesn't deny.
        assert step_stats_display(step_transfer_stats(step_run), "success").label == label


@pytest.mark.asyncio
async def test_dry_run_refuses_a_command_that_ignores_dry_run(tmp_path):
    # rclone config delete deletes the remote even with --dry-run, so a dry run must not
    # start it. The real executor would run rclone if the refusal failed.
    config = tmp_path / "rclone.conf"
    config.write_text("[example]\ntype = local\n", encoding="utf-8")
    job = Job(
        id=5,
        name="config",
        common_args=f"--config {config}",
        env={},
        steps=[
            JobStep(id=1, name="List", command="lsd example:", position=1),
            JobStep(id=2, name="Delete", command="config delete example", position=2),
        ],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)

    step = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
    single = runner.start_job(job, trigger="manual-step-dry-run", dry_run=True, step_id=2)

    for refused in (step, single):
        assert refused.status == "failed"
        assert (
            "rclone config delete can make changes even with --dry-run"
            in Path(refused.log_path).read_text()
        )
    assert config.read_text() == "[example]\ntype = local\n"
    with pytest.raises(CommandPolicyError, match="config delete"):
        await JobRunner(tmp_path / "simple").run_job(job, trigger="manual", dry_run=True)
    assert config.read_text() == "[example]\ntype = local\n"


async def _refused_everywhere(tmp_path, job, *, dry_run):
    """Start the job in both runners; each must refuse it before running anything."""
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)
    trigger = "manual-dry-run" if dry_run else "schedule"
    refused = runner.start_job(job, trigger=trigger, dry_run=dry_run)
    assert refused.status == "failed"
    reason = Path(refused.log_path).read_text()
    with pytest.raises(CommandPolicyError):
        await JobRunner(tmp_path / "simple").run_job(job, trigger=trigger, dry_run=dry_run)
    return reason


@pytest.mark.asyncio
async def test_dry_run_checks_the_nested_command_the_common_arguments_select(tmp_path):
    # "config show" with these common arguments runs rclone config delete show.
    config = tmp_path / "rclone.conf"
    config.write_text("[show]\ntype = local\n", encoding="utf-8")
    job = Job(
        id=6,
        name="config",
        common_args=f"--config {config} delete",
        env={},
        steps=[JobStep(id=1, name="Show", command="config show")],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=True)

    assert "rclone config delete can make changes even with --dry-run" in reason
    assert config.read_text() == "[show]\ntype = local\n"


@pytest.mark.asyncio
async def test_dry_run_refuses_flags_that_overwrite_files(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("keep me\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    job = Job(
        id=7,
        name="hashes",
        common_args=f"--config {tmp_path / 'empty.conf'}",
        env={},
        steps=[
            JobStep(
                id=1,
                name="Hash",
                command=f"hashsum MD5 {tmp_path / 'src'} --output-file {existing}",
            )
        ],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=True)

    assert "--output-file writes a file even with --dry-run" in reason
    assert existing.read_text() == "keep me\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("common", "env"),
    [("--dry-run", {}), ("-n", {}), ("", {"RCLONE_DRY_RUN": "true"})],
)
async def test_a_run_that_asks_for_a_dry_run_itself_is_checked_too(tmp_path, common, env):
    # Run now and the scheduler don't add --dry-run, but the job's own one makes it a dry run
    # all the same, and rclone config delete ignores it.
    config = tmp_path / "rclone.conf"
    config.write_text("[example]\ntype = local\n", encoding="utf-8")
    job = Job(
        id=8,
        name="config",
        common_args=f"{common} --config {config}",
        env=env,
        steps=[JobStep(id=1, name="Delete", command="config delete example")],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=False)

    assert "rclone config delete can make changes even with --dry-run" in reason
    assert config.read_text() == "[example]\ntype = local\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "common",
    ["--dry-run --exclude --dry-run=false", "-n --exclude -n=false"],
)
async def test_a_dry_run_hidden_behind_a_flag_value_is_checked_too(tmp_path, common):
    # rclone takes "--dry-run=false" as --exclude's value, so the --dry-run before it stays on:
    # it is a dry run, and rclone config delete ignores dry runs.
    config = tmp_path / "rclone.conf"
    config.write_text("[example]\ntype = local\n", encoding="utf-8")
    job = Job(
        id=8,
        name="config",
        common_args=f"--config {config} {common}",
        env={},
        steps=[JobStep(id=1, name="Delete", command="config delete example")],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=False)

    assert "rclone config delete can make changes even with --dry-run" in reason
    assert config.read_text() == "[example]\ntype = local\n"


@pytest.mark.asyncio
async def test_bisync_is_not_dry_run_since_it_writes_its_working_files(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    listing = workdir / "listing.lst-dry"
    listing.write_text("earlier\n", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    job = Job(
        id=9,
        name="bisync",
        common_args=f"--config {tmp_path / 'empty.conf'}",
        env={},
        steps=[
            JobStep(
                id=1,
                name="Sync",
                command=f"bisync {tmp_path / 'a'} {tmp_path / 'b'} --resync --workdir {workdir}",
            )
        ],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=True)

    assert "rclone bisync can make changes even with --dry-run" in reason
    assert [path.name for path in workdir.iterdir()] == ["listing.lst-dry"]
    assert listing.read_text() == "earlier\n"


@pytest.mark.asyncio
async def test_a_dry_run_in_a_short_flag_group_with_a_value_is_checked_too(tmp_path):
    # rclone reads "-nf- *.tmp" as -n, then -f with the value "- *.tmp": a dry run.
    config = tmp_path / "rclone.conf"
    config.write_text("[example]\ntype = local\n", encoding="utf-8")
    job = Job(
        id=8,
        name="config",
        common_args=f"--config {config} '-nf- *.tmp'",
        env={},
        steps=[JobStep(id=1, name="Delete", command="config delete example")],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=False)

    assert "rclone config delete can make changes even with --dry-run" in reason
    assert config.read_text() == "[example]\ntype = local\n"


@pytest.mark.asyncio
async def test_a_prune_dry_run_refuses_flags_that_write_files(tmp_path):
    # prune-dated lists with rclone lsf, which gets the common arguments.
    existing = tmp_path / "existing"
    existing.write_text("keep me\n", encoding="utf-8")
    job = Job(
        id=10,
        name="prune",
        common_args=f"--config {tmp_path / 'empty.conf'} --cpuprofile {existing}",
        env={},
        steps=[
            JobStep(
                id=1,
                name="Prune",
                command=f"prune-dated {tmp_path / 'versions'} --keep-days 30 --keep-min 1",
            )
        ],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=True)

    assert "--cpuprofile writes a file even with --dry-run" in reason
    assert existing.read_text() == "keep me\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "common", "env"),
    [
        ("sync /a r:a", "--log-file /tmp/rclone.log", {"RCLONE_DRY_RUN": "false"}),
        ("sync /a r:a", "--dry-run=false --log-file /tmp/rclone.log", {}),
        ("bisync /a r:a", "", {"RCLONE_DRY_RUN": "false"}),
        ("bisync /a r:a", "-n=false", {}),
    ],
)
async def test_runs_that_turn_dry_runs_off_run_as_usual(tmp_path, command, common, env):
    # A real run that says so plainly is no dry run, whatever it does.
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    job = Job(
        id=11,
        name="backup",
        common_args=common,
        env=env,
        steps=[JobStep(id=1, name="Sync", command=command)],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory, executor=executor)
    first = runner.start_job(job, trigger="schedule")
    await runner.wait_for_job_run(first.job_run_id)
    result = await JobRunner(tmp_path / "simple", executor=executor).run_job(
        job, trigger="schedule"
    )

    assert first.status == "running"
    assert result.success
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_dry_run_turns_off_seafiles_library_creation(tmp_path):
    # With create_library on, rclone creates a missing Seafile library just by listing it.
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        return 0

    async def prune(argv, env, log_path, today):
        calls.append(argv)
        return 0

    job = Job(
        id=12,
        name="seafile",
        common_args="",
        env={"RCLONE_CONFIG_S_CREATE_LIBRARY": "true"},
        steps=[
            JobStep(id=1, name="List", command="lsf s:new-library", position=1),
            JobStep(
                id=2,
                name="Prune",
                command="prune-dated s:lib/versions --keep-days 30 --keep-min 1",
                position=2,
            ),
        ],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(
        tmp_path / "logs", session_factory=session_factory, executor=executor, prune=prune
    )
    first = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
    await runner.wait_for_job_run(first.job_run_id)
    await JobRunner(tmp_path / "simple", executor=executor, prune=prune).run_job(
        job, trigger="manual-dry-run", dry_run=True
    )

    listing, pruning = calls[:2]
    assert listing[:3] == ["rclone", "lsf", "--seafile-create-library=false"]
    # prune-dated's rclone flags come last, and go to its rclone lsf.
    assert pruning[-1] == "--seafile-create-library=false"
    assert calls[2:] == calls[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "common",
    ["--seafile-create-library=true", "--seafile-create-library true"],
)
async def test_a_dry_run_refuses_settings_that_would_create_a_seafile_library(tmp_path, common):
    # The job's own flag comes later than the dry run's and would win; so would a connection
    # string's setting.
    for index, command in enumerate(("lsf s:new-library", "lsf s,create_library=true:new-library")):
        job = Job(
            id=13,
            name="seafile",
            common_args=common if "," not in command else "",
            env={},
            steps=[JobStep(id=1, name="List", command=command)],
        )
        (tmp_path / str(index)).mkdir()
        reason = await _refused_everywhere(tmp_path / str(index), job, dry_run=True)
        assert "create_library can create data on the remote" in reason


@pytest.mark.asyncio
async def test_a_real_prune_run_that_turns_dry_runs_off_runs(tmp_path):
    calls = []

    async def prune(argv, env, log_path, today):
        calls.append(argv)
        return 0

    job = Job(
        id=14,
        name="prune",
        common_args="--log-file /tmp/rclone.log",
        env={},
        steps=[
            JobStep(
                id=1,
                name="Prune",
                command="prune-dated r:versions --keep-days 30 --keep-min 1 --dry-run=false",
            )
        ],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory, prune=prune)
    first = runner.start_job(job, trigger="schedule")
    await runner.wait_for_job_run(first.job_run_id)
    result = await JobRunner(tmp_path / "simple", prune=prune).run_job(job, trigger="schedule")

    assert first.status == "running"
    assert result.success
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_a_real_dry_run_creates_no_seafile_library(tmp_path):
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Seafile(BaseHTTPRequestHandler):
        """Just enough of Seafile's API for rclone to log in and list its libraries."""

        def _reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            requests.append(("GET", self.path))
            if self.path.startswith("/api2/server-info"):
                return self._reply({"version": "9.0.0"})
            return self._reply([] if self.path.startswith("/api2/repos") else {})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            requests.append(("POST", self.path))
            if self.path.startswith("/api2/auth-token"):
                return self._reply({"token": "t"})
            return self._reply({"repo_id": "r1", "repo_name": "new-library"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Seafile)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    password = subprocess.run(
        ["rclone", "obscure", "x"], capture_output=True, text=True, check=True
    ).stdout.strip()
    config = tmp_path / "rclone.conf"
    config.write_text(
        f"[s]\ntype = seafile\nurl = http://127.0.0.1:{server.server_port}\n"
        f"user = u@example.com\npass = {password}\ncreate_library = true\n",
        encoding="utf-8",
    )
    job = Job(
        id=15,
        name="seafile",
        common_args=f"--config {config}",
        env={},
        steps=[JobStep(id=1, name="List", command="lsf s:new-library")],
    )
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(tmp_path / "logs", session_factory=session_factory)
    try:
        first = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
        await runner.wait_for_job_run(first.job_run_id)
    finally:
        server.shutdown()

    assert ("GET", "/api2/repos/") in requests  # rclone did look for the library...
    assert ("POST", "/api2/repos/") not in requests  # ...and didn't create it.

    # An alias naming a connection string with create_library: refused before rclone starts.
    requests.clear()
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[a]\ntype = alias\nremote = s,create_library=true:new-library\n",
        encoding="utf-8",
    )
    job.steps = [JobStep(id=1, name="List", command="lsf a:")]
    refused = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
    assert refused.status == "failed"
    assert "create_library" in Path(refused.log_path).read_text()
    assert requests == []

    # Spelled bare, or built from another value: refused too.
    for remote in ("s,create_library:new-library", "s,%(option)s=true:new-library"):
        config.write_text(
            config.read_text(encoding="utf-8").split("\n[a]")[0]
            + f"\n[a]\ntype = alias\noption = create_library\nremote = {remote}\n",
            encoding="utf-8",
        )
        refused = runner.start_job(job, trigger="manual-dry-run", dry_run=True)
        assert refused.status == "failed"
        assert requests == []


@pytest.mark.asyncio
async def test_a_prune_run_is_checked_as_its_rclone_reads_its_flags(tmp_path):
    # prune-dated leaves --exclude out of its rclone lsf, with the value that follows it: so
    # "--dry-run=false" goes, and rclone gets --dry-run. It's a dry run, and --cpuprofile would
    # overwrite the file.
    existing = tmp_path / "existing"
    existing.write_text("keep me\n", encoding="utf-8")
    job = Job(
        id=16,
        name="prune",
        common_args=f"--dry-run --exclude --dry-run=false --cpuprofile={existing}",
        env={},
        steps=[
            JobStep(
                id=1,
                name="Prune",
                command=f"prune-dated {tmp_path / 'versions'} --keep-days 30 --keep-min 1",
            )
        ],
    )

    reason = await _refused_everywhere(tmp_path, job, dry_run=False)

    assert "--cpuprofile writes a file even with --dry-run" in reason
    assert existing.read_text() == "keep me\n"


@pytest.mark.parametrize("where", ["config", "environment"])
def test_a_dry_run_refuses_a_remote_that_names_seafile_library_creation(tmp_path, where):
    # An alias's remote is a connection string, whose create_library beats the dry run's flag.
    from app.core.commands import build_step_argv, dry_run_refusal

    config = tmp_path / "rclone.conf"
    env = {}
    if where == "config":
        config.write_text(
            "[s]\ntype = seafile\n\n[a]\ntype = alias\nremote = s,create_library=true:lib\n",
            encoding="utf-8",
        )
    else:
        config.write_text("", encoding="utf-8")
        env = {"RCLONE_CONFIG_A_TYPE": "alias", "RCLONE_CONFIG_A_REMOTE": "s,create_library=true:"}
    argv = build_step_argv("lsf a:", f"--config {config}", env)

    assert "create_library can create data on the remote" in dry_run_refusal(argv, env)
    # A config whose create_library is a plain setting is beaten by the flag: no refusal.
    config.write_text("[s]\ntype = seafile\ncreate_library = true\n", encoding="utf-8")
    assert dry_run_refusal(build_step_argv("lsf s:", f"--config {config}", {}), {}) is None


_UNSAFE_ALIAS = "[s]\ntype = seafile\n\n[a]\ntype = alias\nremote = s,create_library=true:lib\n"


def test_every_config_a_dry_run_may_read_is_checked(tmp_path):
    from app.core.commands import build_step_argv, dry_run_refusal

    def refusal(common, env):
        env = {"HOME": str(tmp_path / "nohome"), "XDG_CONFIG_HOME": str(tmp_path / "noxdg")} | env
        return dry_run_refusal(build_step_argv("lsf a:", common, env), env)

    actual = tmp_path / "actual.conf"
    actual.write_text(_UNSAFE_ALIAS, encoding="utf-8")
    (tmp_path / "empty.conf").write_text("", encoding="utf-8")
    # rclone takes "--config=empty.conf" as --exclude's value, and reads actual.conf.
    common = f"--dry-run --config {actual} --exclude --config={tmp_path / 'empty.conf'}"
    assert "create_library" in refusal(common, {})

    # Without --config or RCLONE_CONFIG, rclone also looks in ~/.config, XDG_CONFIG_HOME or not.
    home = tmp_path / "home"
    (home / ".config" / "rclone").mkdir(parents=True)
    (home / ".config" / "rclone" / "rclone.conf").write_text(_UNSAFE_ALIAS, encoding="utf-8")
    assert "create_library" in refusal("", {"HOME": str(home)})

    # The whole file, however long.
    padded = tmp_path / "padded.conf"
    padded.write_text("# padding\n" * 200_000 + _UNSAFE_ALIAS, encoding="utf-8")
    assert padded.stat().st_size > 1024 * 1024
    assert "create_library" in refusal(f"--config {padded}", {})

    # A config without it is fine.
    assert refusal(f"--config {tmp_path / 'empty.conf'}", {}) is None


@pytest.mark.parametrize(
    "alias",
    [
        # A bare parameter means true.
        "[a]\ntype = alias\nremote = s,create_library:lib\n",
        # The config can build the value from another, which the check can't follow.
        "[a]\ntype = alias\noption = create_library\nremote = s,%(option)s=true:lib\n",
        "[a]\ntype = alias\nx = create\nremote = s,%(x)s_library=true:lib\n",
    ],
)
def test_a_dry_run_refuses_library_creation_however_the_config_spells_it(tmp_path, alias):
    from app.core.commands import build_step_argv, dry_run_refusal

    config = tmp_path / "rclone.conf"
    config.write_text("[s]\ntype = seafile\n\n" + alias, encoding="utf-8")
    env = {"HOME": str(tmp_path / "nohome")}
    argv = build_step_argv("lsf a:", f"--config {config}", env)

    assert "create_library" in dry_run_refusal(argv, env)


def test_a_dry_run_refuses_a_bare_library_creation_parameter_in_the_environment(tmp_path):
    from app.core.commands import build_step_argv, dry_run_refusal

    env = {
        "HOME": str(tmp_path / "nohome"),
        "RCLONE_CONFIG_A_TYPE": "alias",
        "RCLONE_CONFIG_A_REMOTE": "s,create_library:lib",
    }
    argv = build_step_argv("lsf a:", f"--config {tmp_path / 'empty.conf'}", env)

    assert "create_library" in dry_run_refusal(argv, env)
    # The option as a remote's own setting is beaten by the dry run's flag: no refusal.
    env = {"HOME": str(tmp_path / "nohome"), "RCLONE_CONFIG_S_CREATE_LIBRARY": "true"}
    argv = build_step_argv("lsf s:", f"--config {tmp_path / 'empty.conf'}", env)
    assert dry_run_refusal(argv, env) is None

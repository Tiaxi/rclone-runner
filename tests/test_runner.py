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
        ["rclone", "sync", *STATS_FLAGS, "--dry-run", "--fast-list", "/source", "secret:/source"]
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

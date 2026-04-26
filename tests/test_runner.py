import asyncio
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.models import Job, JobStep
from app.core.runner import JobRunner, LiveJobRunner
from app.db import Base, JobRunRecord, JobStepRunRecord
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
    assert calls == [["rclone", "sync", "--dry-run", "--fast-list", "/source", "secret:/source"]]


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
    assert calls == [["rclone", "sync", "/second", "secret:/second"]]


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


def _session_factory(database_path: Path, expire_on_commit: bool = False):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=expire_on_commit
    )

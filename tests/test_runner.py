import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.models import Job, JobStep, utc_now
from app.core.runner import JobRunner, LiveJobRunner, subprocess_executor
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
                '{"deleted_files": 2, "transferred_bytes": 1572864, "transferred_files": 7}'
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

        with session_factory() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            assert job_run.status == "success"
        assert notified == [first_step.job_run_id]


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


def _session_factory(database_path: Path, expire_on_commit: bool = False):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=expire_on_commit
    )

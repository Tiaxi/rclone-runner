import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.models import Job, JobStep
from app.core.runner import JobRunner
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

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from app.core.commands import build_rclone_argv
from app.core.models import Job, JobRunResult, JobStep, StepRunResult, utc_now

Executor = Callable[[list[str], dict[str, str], Path], Awaitable[int]]


async def subprocess_executor(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ | env
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=merged_env,
    )
    assert process.stdout is not None
    with log_path.open("wb") as log_file:
        async for chunk in process.stdout:
            log_file.write(chunk)
            log_file.flush()
    return await process.wait()


class JobRunner:
    def __init__(self, log_root: Path, executor: Executor = subprocess_executor) -> None:
        self._log_root = log_root
        self._executor = executor
        self._active_job_ids: set[int] = set()
        self._lock = asyncio.Lock()

    async def run_job(
        self, job: Job, trigger: str, dry_run: bool = False, step_id: int | None = None
    ) -> JobRunResult:
        async with self._lock:
            if job.id in self._active_job_ids:
                now = utc_now()
                return JobRunResult(
                    job_id=job.id,
                    job_name=job.name,
                    trigger=trigger,
                    status="skipped",
                    started_at=now,
                    ended_at=now,
                )
            self._active_job_ids.add(job.id)

        started_at = utc_now()
        step_runs: list[StepRunResult] = []
        status = "success"
        try:
            run_stamp = _stamp(started_at)
            for step in _selected_steps(job, step_id):
                argv = build_rclone_argv(step.command, _common_args(job, dry_run), job.env)
                step_started_at = utc_now()
                log_path = self._log_root / f"job-{job.id}" / f"{run_stamp}-step-{step.id}.log"
                exit_code = await self._executor(argv, job.env, log_path)
                step_ended_at = utc_now()
                step_runs.append(
                    StepRunResult(
                        step_id=step.id,
                        step_name=step.name,
                        argv=argv,
                        started_at=step_started_at,
                        ended_at=step_ended_at,
                        exit_code=exit_code,
                        log_path=log_path,
                    )
                )
                if exit_code != 0:
                    status = "failed"
                    break
        finally:
            async with self._lock:
                self._active_job_ids.remove(job.id)

        return JobRunResult(
            job_id=job.id,
            job_name=job.name,
            trigger=trigger,
            status=status,
            started_at=started_at,
            ended_at=utc_now(),
            step_runs=step_runs,
        )


def _stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _common_args(job: Job, dry_run: bool) -> str:
    if not dry_run:
        return job.common_args
    if "--dry-run" in job.common_args.split():
        return job.common_args
    return f"--dry-run {job.common_args}".strip()


def _selected_steps(job: Job, step_id: int | None) -> list[JobStep]:
    steps = sorted(job.steps, key=lambda item: item.position)
    if step_id is None:
        return steps
    return [step for step in steps if step.id == step_id]

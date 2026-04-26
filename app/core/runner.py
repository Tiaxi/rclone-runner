from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from app.core.commands import build_rclone_argv
from app.core.models import Job, JobRunResult, JobStep, StepRunResult, utc_now
from app.db import JobRunRecord, JobStepRunRecord

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
    try:
        with log_path.open("wb") as log_file:
            async for chunk in process.stdout:
                log_file.write(chunk)
                log_file.flush()
        return await process.wait()
    except asyncio.CancelledError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise


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


class LiveJobRunner:
    def __init__(
        self, log_root: Path, session_factory, executor: Executor = subprocess_executor
    ) -> None:
        self._log_root = log_root
        self._session_factory = session_factory
        self._executor = executor
        self._active_job_ids: set[int] = set()
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def start_job(
        self, job: Job, trigger: str, dry_run: bool = False, step_id: int | None = None
    ) -> JobStepRunRecord | None:
        now = utc_now()
        selected_steps = _selected_steps(job, step_id)
        if job.id in self._active_job_ids or not selected_steps:
            status = "skipped" if job.id in self._active_job_ids else "success"
            with self._session_factory() as db:
                db.add(
                    JobRunRecord(
                        job_id=job.id,
                        job_name=job.name,
                        trigger=trigger,
                        status=status,
                        started_at=now,
                        ended_at=now,
                    )
                )
                db.commit()
            return None

        self._active_job_ids.add(job.id)
        run_stamp = _stamp(now)
        first_step = selected_steps[0]
        first_argv = build_rclone_argv(first_step.command, _common_args(job, dry_run), job.env)
        first_log_path = self._log_root / f"job-{job.id}" / f"{run_stamp}-step-{first_step.id}.log"
        first_log_path.parent.mkdir(parents=True, exist_ok=True)
        first_log_path.touch(exist_ok=True)

        with self._session_factory() as db:
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger=trigger,
                status="running",
                started_at=now,
                ended_at=None,
            )
            db.add(job_run)
            db.flush()
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=first_step.id,
                step_name=first_step.name,
                argv_json=json.dumps(first_argv),
                status="running",
                exit_code=None,
                log_path=str(first_log_path),
                started_at=now,
                ended_at=None,
            )
            db.add(step_run)
            db.commit()
            db.refresh(step_run)
            job_run_id = job_run.id
            step_run_id = step_run.id
            step_run_job_run_id = step_run.job_run_id
            step_run_step_id = step_run.step_id
            step_run_step_name = step_run.step_name
            step_run_argv_json = step_run.argv_json
            step_run_status = step_run.status
            step_run_exit_code = step_run.exit_code
            step_run_log_path = step_run.log_path
            step_run_started_at = step_run.started_at
            step_run_ended_at = step_run.ended_at
            db.expunge(step_run)

        task = asyncio.create_task(
            self._run_job_background(job_run_id, job, dry_run, selected_steps, run_stamp)
        )
        self._tasks[job_run_id] = task
        task.add_done_callback(lambda completed: self._task_done(job.id, job_run_id, completed))
        return JobStepRunRecord(
            id=step_run_id,
            job_run_id=step_run_job_run_id,
            step_id=step_run_step_id,
            step_name=step_run_step_name,
            argv_json=step_run_argv_json,
            status=step_run_status,
            exit_code=step_run_exit_code,
            log_path=step_run_log_path,
            started_at=step_run_started_at,
            ended_at=step_run_ended_at,
        )

    def cancel_job_run(self, job_run_id: int) -> bool:
        task = self._tasks.get(job_run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait_for_job_run(self, job_run_id: int) -> None:
        task = self._tasks.get(job_run_id)
        if task is None:
            return
        with suppress(asyncio.CancelledError):
            await task

    async def _run_job_background(
        self,
        job_run_id: int,
        job: Job,
        dry_run: bool,
        selected_steps: list[JobStep],
        run_stamp: str,
    ) -> None:
        job_status = "success"
        step_run: JobStepRunRecord | None = None
        try:
            for index, step in enumerate(selected_steps):
                if index == 0:
                    step_run = self._get_first_step_run(job_run_id)
                else:
                    step_run = self._create_step_run(job_run_id, job, step, dry_run, run_stamp)

                exit_code = await self._executor(
                    json.loads(step_run.argv_json), job.env, Path(step_run.log_path)
                )
                step_status = "success" if exit_code == 0 else "failed"
                self._finish_step_run(step_run.id, step_status, exit_code)
                if exit_code != 0:
                    job_status = "failed"
                    break
        except asyncio.CancelledError:
            job_status = "canceled"
            self._cancel_running_step(job_run_id)
            raise
        except Exception:
            job_status = "failed"
            if step_run is not None:
                self._finish_step_run(step_run.id, "failed", None)
        finally:
            self._finish_job_run(job_run_id, job_status)

    def _get_first_step_run(self, job_run_id: int) -> JobStepRunRecord:
        with self._session_factory() as db:
            step_run = (
                db.query(JobStepRunRecord)
                .filter_by(job_run_id=job_run_id)
                .order_by(JobStepRunRecord.id)
                .first()
            )
            if step_run is None:
                raise RuntimeError(f"missing first step run for job run {job_run_id}")
            db.expunge(step_run)
            return step_run

    def _create_step_run(
        self, job_run_id: int, job: Job, step: JobStep, dry_run: bool, run_stamp: str
    ) -> JobStepRunRecord:
        now = utc_now()
        argv = build_rclone_argv(step.command, _common_args(job, dry_run), job.env)
        log_path = self._log_root / f"job-{job.id}" / f"{run_stamp}-step-{step.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        with self._session_factory() as db:
            step_run = JobStepRunRecord(
                job_run_id=job_run_id,
                step_id=step.id,
                step_name=step.name,
                argv_json=json.dumps(argv),
                status="running",
                exit_code=None,
                log_path=str(log_path),
                started_at=now,
                ended_at=None,
            )
            db.add(step_run)
            db.commit()
            db.refresh(step_run)
            db.expunge(step_run)
            return step_run

    def _finish_step_run(self, step_run_id: int, status: str, exit_code: int | None) -> None:
        with self._session_factory() as db:
            step_run = db.get(JobStepRunRecord, step_run_id)
            if step_run is None or step_run.status != "running":
                return
            step_run.status = status
            step_run.exit_code = exit_code
            step_run.ended_at = utc_now()
            db.commit()

    def _cancel_running_step(self, job_run_id: int) -> None:
        with self._session_factory() as db:
            step_run = (
                db.query(JobStepRunRecord)
                .filter_by(job_run_id=job_run_id, status="running")
                .order_by(JobStepRunRecord.started_at.desc())
                .first()
            )
            if step_run is None:
                return
            step_run.status = "canceled"
            step_run.exit_code = None
            step_run.ended_at = utc_now()
            db.commit()

    def _finish_job_run(self, job_run_id: int, status: str) -> None:
        with self._session_factory() as db:
            job_run = db.get(JobRunRecord, job_run_id)
            if job_run is None or job_run.status != "running":
                return
            job_run.status = status
            job_run.ended_at = utc_now()
            db.commit()

    def _task_done(self, job_id: int, job_run_id: int, task: asyncio.Task[None]) -> None:
        self._active_job_ids.discard(job_id)
        self._tasks.pop(job_run_id, None)
        if not task.cancelled():
            task.exception()


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

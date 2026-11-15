from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from app.core.commands import CommandPolicyError, build_rclone_argv
from app.core.models import Job, JobRunResult, JobStep, StepRunResult, utc_now
from app.core.rclone_stats import stats_from_log, stats_to_json
from app.db import ConsoleRunRecord, JobRunRecord, JobStepRunRecord

logger = logging.getLogger(__name__)

INTERRUPTED_LOG_NOTE = (
    "\n[rclone-runner] Interrupted: the app restarted while this step was running.\n"
)

Executor = Callable[[list[str], dict[str, str], Path], Awaitable[int]]
NotificationCallback = Callable[[int], Awaitable[None]]


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
                        transfer_stats=stats_from_log(log_path),
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
        self,
        log_root: Path,
        session_factory,
        executor: Executor = subprocess_executor,
        notification_callback: NotificationCallback | None = None,
    ) -> None:
        self._log_root = log_root
        self._session_factory = session_factory
        self._executor = executor
        self._notification_callback = notification_callback
        self._active_job_ids: set[int] = set()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._notification_tasks: set[asyncio.Task[None]] = set()

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

        run_stamp = _stamp(now)
        common_args = _common_args(job, dry_run)
        planned_steps: list[tuple[JobStep, list[str]]] = []
        for step in selected_steps:
            try:
                planned_steps.append((step, build_rclone_argv(step.command, common_args, job.env)))
            except CommandPolicyError as exc:
                return self._record_invalid_run(job, trigger, step, run_stamp, exc)

        self._active_job_ids.add(job.id)
        try:
            first_step, first_argv = planned_steps[0]
            first_log_path = self._step_log_path(job, first_step, run_stamp)
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
                first_step_run = _detached_copy(step_run)

            task = asyncio.create_task(
                self._run_job_background(job_run_id, job, planned_steps, run_stamp)
            )
        except BaseException:
            self._active_job_ids.discard(job.id)
            raise
        self._tasks[job_run_id] = task
        task.add_done_callback(lambda completed: self._task_done(job.id, job_run_id, completed))
        return first_step_run

    def recover_interrupted_runs(self) -> list[int]:
        """Close out runs left "running" by a previous process and notify about them.

        Only call this at startup, before any job has been started by this process.
        """
        now = utc_now()
        with self._session_factory() as db:
            job_runs = db.query(JobRunRecord).filter_by(status="running").all()
            job_run_ids = [job_run.id for job_run in job_runs]
            for job_run in job_runs:
                job_run.status = "failed"
                job_run.ended_at = now
            for step_run in db.query(JobStepRunRecord).filter_by(status="running").all():
                _append_log(Path(step_run.log_path), INTERRUPTED_LOG_NOTE)
                step_run.status = "failed"
                step_run.exit_code = None
                step_run.ended_at = now
            for console_run in db.query(ConsoleRunRecord).filter_by(status="running").all():
                console_run.status = "canceled"
                console_run.exit_code = None
                console_run.ended_at = now
            db.commit()
        if job_run_ids:
            logger.warning("Marked interrupted job runs as failed: %s", job_run_ids)
        for job_run_id in job_run_ids:
            self._notify_soon(job_run_id)
        return job_run_ids

    def _record_invalid_run(
        self,
        job: Job,
        trigger: str,
        step: JobStep,
        run_stamp: str,
        error: CommandPolicyError,
    ) -> JobStepRunRecord:
        now = utc_now()
        log_path = self._step_log_path(job, step, run_stamp)
        _append_log(
            log_path,
            f"[rclone-runner] Could not start step {step.name!r}: {error}\n"
            "[rclone-runner] No steps were run.\n",
        )
        with self._session_factory() as db:
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger=trigger,
                status="failed",
                started_at=now,
                ended_at=now,
            )
            db.add(job_run)
            db.flush()
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=step.id,
                step_name=step.name,
                argv_json=json.dumps([]),
                status="failed",
                exit_code=None,
                log_path=str(log_path),
                started_at=now,
                ended_at=now,
            )
            db.add(step_run)
            db.commit()
            db.refresh(step_run)
            job_run_id = job_run.id
            result = _detached_copy(step_run)
        self._notify_soon(job_run_id)
        return result

    def _notify_soon(self, job_run_id: int) -> None:
        if self._notification_callback is None:
            return
        callback = self._notification_callback

        async def notify() -> None:
            with suppress(Exception):
                await callback(job_run_id)

        task = asyncio.create_task(notify())
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    def _step_log_path(self, job: Job, step: JobStep, run_stamp: str) -> Path:
        return self._log_root / f"job-{job.id}" / f"{run_stamp}-step-{step.id}.log"

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
        planned_steps: list[tuple[JobStep, list[str]]],
        run_stamp: str,
    ) -> None:
        job_status = "success"
        step_run: JobStepRunRecord | None = None
        try:
            for index, (step, argv) in enumerate(planned_steps):
                if index == 0:
                    step_run = self._get_first_step_run(job_run_id)
                else:
                    step_run = self._create_step_run(job_run_id, job, step, argv, run_stamp)

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
        except Exception as exc:
            job_status = "failed"
            logger.exception("Job run %s failed unexpectedly", job_run_id)
            if step_run is not None:
                _append_log(
                    Path(step_run.log_path),
                    f"\n[rclone-runner] Step failed to run: {exc!r}\n",
                )
                self._finish_step_run(step_run.id, "failed", None)
        finally:
            self._finish_job_run(job_run_id, job_status)
            if self._notification_callback is not None:
                with suppress(Exception):
                    await self._notification_callback(job_run_id)

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
        self, job_run_id: int, job: Job, step: JobStep, argv: list[str], run_stamp: str
    ) -> JobStepRunRecord:
        now = utc_now()
        log_path = self._step_log_path(job, step, run_stamp)
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
            stats = stats_from_log(Path(step_run.log_path))
            step_run.transfer_stats_json = stats_to_json(stats) if stats is not None else None
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


def _detached_copy(step_run: JobStepRunRecord) -> JobStepRunRecord:
    return JobStepRunRecord(
        id=step_run.id,
        job_run_id=step_run.job_run_id,
        step_id=step_run.step_id,
        step_name=step_run.step_name,
        argv_json=step_run.argv_json,
        status=step_run.status,
        exit_code=step_run.exit_code,
        log_path=step_run.log_path,
        started_at=step_run.started_at,
        ended_at=step_run.ended_at,
    )


def _append_log(path: Path, text: str) -> None:
    """Add a note to a run log. Best effort: a full or unwritable disk must not stop
    run records from being saved, or startup recovery from completing."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)
    except OSError:
        logger.warning("Could not write to run log %s", path, exc_info=True)


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

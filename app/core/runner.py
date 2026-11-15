from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path

from app.config import settings
from app.core.aio import run_to_completion, stop_process
from app.core.commands import (
    CommandPolicyError,
    build_step_argv,
    child_environment,
    run_date,
    run_variables,
)
from app.core.live_control import (
    RcError,
    RunControl,
    configured_rate,
    normalize_rate,
    pause_rate,
    set_rclone_bandwidth,
    supports_live_control,
    with_flags,
)
from app.core.models import Job, JobRunResult, JobStep, StepRunResult, utc_now
from app.core.prune import is_prune_command, may_turn_off_dry_run, run_prune
from app.core.rclone_stats import STATS_UNAVAILABLE_JSON, stats_from_log, stats_to_json
from app.db import ConsoleRunRecord, JobRunRecord, JobStepRunRecord

logger = logging.getLogger(__name__)

INTERRUPTED_LOG_NOTE = (
    "\n[rclone-runner] Interrupted: the app restarted while this step was running.\n"
)

Executor = Callable[[list[str], dict[str, str], Path], Awaitable[int]]
# Runs a built-in prune-dated step: (argv, job env, log path, the run's date) -> exit code.
PruneExecutor = Callable[[list[str], dict[str, str], Path, date], Awaitable[int]]
NotificationCallback = Callable[[int], Awaitable[None]]


OUTPUT_READ_SIZE = 64 * 1024
# How long a live control request waits for rclone to confirm before returning anyway.
APPLY_WAIT_SECONDS = 3.0
TERMINATE_TIMEOUT_SECONDS = 5


async def subprocess_executor(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = child_environment(env)
    # Open the log before starting rclone so a log error cannot leave it running.
    with log_path.open("wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged_env,
        )
        assert process.stdout is not None
        try:
            # Read fixed-size chunks rather than lines: line iteration fails on lines longer
            # than the stream limit, which would abandon a still-running rclone.
            while chunk := await process.stdout.read(OUTPUT_READ_SIZE):
                log_file.write(chunk)
                log_file.flush()
            return await process.wait()
        except BaseException as exc:
            cancelled = await run_to_completion(_stop_process(process))
            if cancelled and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError from exc
            raise


async def prune_executor(argv: list[str], env: dict[str, str], log_path: Path, today: date) -> int:
    return await run_prune(argv, child_environment(env), log_path, today, settings.timezone)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    await stop_process(process, TERMINATE_TIMEOUT_SECONDS)


class JobRunner:
    def __init__(
        self,
        log_root: Path,
        executor: Executor = subprocess_executor,
        prune: PruneExecutor = prune_executor,
    ) -> None:
        self._log_root = log_root
        self._executor = executor
        self._prune = prune
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
            today = run_date(started_at, settings.timezone)
            for step in _selected_steps(job, step_id):
                argv = _step_argv(
                    step.command,
                    _common_args(job, dry_run),
                    _expansion_env(job, started_at),
                    dry_run,
                )
                step_started_at = utc_now()
                log_path = self._log_root / f"job-{job.id}" / f"{run_stamp}-step-{step.id}.log"
                if is_prune_command(argv):
                    exit_code = await self._prune(argv, job.env, log_path, today)
                else:
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
        control_dir: Path | None = None,
        prune: PruneExecutor = prune_executor,
    ) -> None:
        self._log_root = log_root
        self._session_factory = session_factory
        self._executor = executor
        self._prune = prune
        self._notification_callback = notification_callback
        # Where steps' remote control sockets go; None turns live control off. It must be a
        # private directory: anyone who can open a socket there controls that rclone.
        self._control_dir = control_dir
        self._active_job_ids: set[int] = set()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._controls: dict[int, RunControl] = {}
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
        # Every step of the run sees the same ${RUN_DATE}, even across midnight.
        env = _expansion_env(job, now)
        planned_steps: list[tuple[JobStep, list[str]]] = []
        for step in selected_steps:
            try:
                planned_steps.append((step, _step_argv(step.command, common_args, env, dry_run)))
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

            # A prune-dated step counts days from the run's ${RUN_DATE} day.
            today = run_date(now, settings.timezone)
            task = asyncio.create_task(
                self._run_job_background(job_run_id, job, planned_steps, run_stamp, today)
            )
        except BaseException:
            self._active_job_ids.discard(job.id)
            raise
        self._tasks[job_run_id] = task
        if self._control_dir is not None:
            self._controls[job_run_id] = RunControl()
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
        # Cancel once: a repeated request would only interrupt the shutdown in progress.
        if not task.cancelling():
            task.cancel()
        return True

    def control_state(self, job_run_id: int) -> dict[str, object] | None:
        """Live control state of a running job run, or None when it has none."""
        control = self._controls.get(job_run_id)
        return control.state() if control is not None else None

    async def set_bandwidth(self, job_run_id: int, rate: str | None) -> None:
        """Set the run's bandwidth limit; None goes back to the job's own setting.

        Raises RateError for a limit rclone would not accept, KeyError when the run has no
        live control, and RcError when the running rclone could not be reached. The choice
        is kept either way and applies to the run's later steps.
        """
        control = self._controls[job_run_id]
        control.limit = normalize_rate(rate) if rate is not None else None
        await self._apply(control)

    async def pause(self, job_run_id: int) -> None:
        control = self._controls[job_run_id]
        control.paused = True
        await self._apply(control)

    async def resume(self, job_run_id: int) -> None:
        control = self._controls[job_run_id]
        control.paused = False
        await self._apply(control)

    async def _apply(self, control: RunControl) -> None:
        socket_path = control.socket_path
        if socket_path is None:
            # Between steps, or a step that cannot be controlled: the next step starts with
            # the new setting.
            return
        task = control.apply_task
        if task is None or task.done() or control.apply_socket != socket_path:
            # A task left from an earlier step stops by itself; this step gets its own.
            task = asyncio.create_task(_send_latest_rate(control, socket_path))
            task.add_done_callback(_log_apply_failure)
            control.apply_task = task
            control.apply_socket = socket_path
        try:
            # rclone may take a while to answer (see live_control); it keeps the change.
            await asyncio.wait_for(asyncio.shield(task), APPLY_WAIT_SECONDS)
        except TimeoutError:
            return
        except (OSError, RcError) as exc:
            if control.socket_path != socket_path:
                return  # The step ended meanwhile; the next one starts with the setting.
            raise RcError(f"could not reach the running rclone: {exc}") from exc

    async def _apply_quietly(self, control: RunControl) -> None:
        try:
            await self._apply(control)
        except RcError as exc:
            logger.warning("Could not apply the run's bandwidth setting to a new step: %s", exc)

    def _start_background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

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
        today: date,
    ) -> None:
        job_status = "success"
        step_run: JobStepRunRecord | None = None
        try:
            for index, (step, argv) in enumerate(planned_steps):
                if index == 0:
                    step_run = self._get_first_step_run(job_run_id)
                else:
                    step_run = self._create_step_run(job_run_id, job, step, argv, run_stamp)

                exit_code = await self._run_step(job_run_id, step_run, job.env, today)
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

    async def _run_step(
        self, job_run_id: int, step_run: JobStepRunRecord, env: dict[str, str], today: date
    ) -> int:
        argv = json.loads(step_run.argv_json)
        log_path = Path(step_run.log_path)
        control = self._controls.get(job_run_id)
        if is_prune_command(argv):
            # Not an rclone process with a control socket: bandwidth changes and pauses wait
            # for the next step.
            if control is not None:
                control.configured = None
            return await self._prune(argv, env, log_path, today)
        if control is None or self._control_dir is None:
            return await self._executor(argv, env, log_path)
        control.configured = configured_rate(argv)
        control.pause_rate = pause_rate(argv)
        if not supports_live_control(argv):
            return await self._executor(argv, env, log_path)
        socket_path = self._control_dir / f"step-{step_run.id}.sock"
        # rclone refuses to start when a socket from a killed process is still there.
        socket_path.unlink(missing_ok=True)
        # The stored argv stays the job's own command, so it can be copied and rerun as is.
        spawn_argv = with_flags(argv, control.spawn_args(socket_path))
        control.socket_path = socket_path
        if control.target_rate(for_new_process=True) is not None:
            # The job's own --bwlimit comes after ours on the command line and wins, so send
            # the run's setting through the socket as soon as rclone is listening.
            self._start_background(self._apply_quietly(control))
        try:
            return await self._executor(spawn_argv, env, log_path)
        finally:
            control.socket_path = None
            socket_path.unlink(missing_ok=True)

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
            step_run.transfer_stats_json = (
                stats_to_json(stats) if stats is not None else STATS_UNAVAILABLE_JSON
            )
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
        self._controls.pop(job_run_id, None)
        if not task.cancelled():
            task.exception()


async def _send_latest_rate(control: RunControl, socket_path: Path) -> None:
    """Send limit changes to one rclone until it has the latest, one call at a time."""
    sent = None
    while control.socket_path == socket_path and (rate := control.target_rate()) != sent:
        await set_rclone_bandwidth(socket_path, rate)
        sent = rate


def _log_apply_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Could not change a running rclone's bandwidth limit: %s", exc)


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


def _expansion_env(job: Job, started_at: datetime) -> dict[str, str]:
    return run_variables(started_at, settings.timezone) | job.env


def _step_argv(command: str, common_args: str, env: dict[str, str], dry_run: bool) -> list[str]:
    argv = build_step_argv(command, common_args, env)
    if dry_run and may_turn_off_dry_run(argv):
        # rclone uses the last value given, so the step's own --dry-run=false would beat the
        # one a dry run adds and make it a real run.
        raise CommandPolicyError(
            "a dry run can't start: the step or the common arguments turn dry runs off "
            "(--dry-run=false or -n=false)"
        )
    return argv


def _common_args(job: Job, dry_run: bool) -> str:
    if not dry_run:
        return job.common_args
    # Always first, even when the common arguments name it already: there it may be another
    # flag's value (--exclude --dry-run), and first it can't be.
    return f"--dry-run {job.common_args}".strip()


def _selected_steps(job: Job, step_id: int | None) -> list[JobStep]:
    steps = sorted(job.steps, key=lambda item: item.position)
    if step_id is None:
        return steps
    return [step for step in steps if step.id == step_id]

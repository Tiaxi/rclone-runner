from __future__ import annotations

import asyncio
import json
import logging
import secrets
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
    dry_run_refusal,
    effective_run_date,
    requests_dry_run,
    run_date,
    run_variables,
    with_dry_run_safeguards,
)
from app.core.live_control import (
    RcError,
    RunControl,
    job_bandwidth,
    may_set_bwlimit,
    normalize_rate,
    parse_bandwidth,
    pause_rate,
    pause_rate_for,
    rc_call,
    read_rclone_options,
    same_rate,
    set_rclone_bandwidth,
    set_rclone_bwlimit_option,
    supports_live_control,
    with_flags,
)
from app.core.models import Job, JobRunResult, JobStep, StepRunResult, utc_now
from app.core.prune import is_prune_command, may_turn_off_dry_run, run_prune
from app.core.rclone_stats import STATS_UNAVAILABLE_JSON, stats_to_json, step_stats_from_log
from app.db import ConsoleRunRecord, JobRunRecord, JobStepRunRecord

logger = logging.getLogger(__name__)

INTERRUPTED_LOG_NOTE = (
    "\n[rclone-runner] Interrupted: the app restarted while this step was running.\n"
)

Executor = Callable[[list[str], dict[str, str], Path], Awaitable[int]]
# Runs a built-in prune-dated step: (argv, job env, log path, the run's date) -> exit code.
PruneExecutor = Callable[[list[str], dict[str, str], Path, date], Awaitable[int]]
NotificationCallback = Callable[[int], Awaitable[None]]


# Subcommands whose transfer totals the run pages, history and emails show. rclone prints them
# only at its INFO level (-v) unless asked for them at NOTICE, the level it logs at by default,
# and delete, deletefile and purge print none at all without --stats.
STATS_SUBCOMMANDS = {
    "bisync",
    "copy",
    "copyto",
    "delete",
    "deletefile",
    "move",
    "moveto",
    "purge",
    "sync",
}
# Each with the environment variable that sets it instead: then the flag, which would win, is
# left out.
STATS_FLAGS = {"--stats-log-level=NOTICE": "RCLONE_STATS_LOG_LEVEL", "--stats=1m": "RCLONE_STATS"}

OUTPUT_READ_SIZE = 64 * 1024
# How long a live control request waits for rclone to confirm before returning anyway.
APPLY_WAIT_SECONDS = 3.0
TERMINATE_TIMEOUT_SECONDS = 5
# How often the runner checks the rate of a step whose timetable it follows in rclone's place:
# rclone checks its own each minute.
FOLLOW_TIMETABLE_SECONDS = 5.0
FOLLOW_STOP_SECONDS = 1.0


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
            run_stamp = _run_stamp(started_at)
            env = _expansion_env(job, started_at)
            today = _run_day(env, started_at)
            # Check every step before running any.
            planned_steps = [
                (step, _step_argv(step.command, _common_args(job, dry_run), env, dry_run))
                for step in _selected_steps(job, step_id)
            ]
            for step, argv in planned_steps:
                step_started_at = utc_now()
                log_path = _step_log_path(self._log_root, job, step, run_stamp)
                process_argv = with_dry_run_safeguards(argv, env)
                if is_prune_command(argv):
                    exit_code = await self._prune(process_argv, job.env, log_path, today)
                else:
                    exit_code = await self._executor(
                        _process_argv(process_argv, job.env), job.env, log_path
                    )
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
                        transfer_stats=step_stats_from_log(argv, log_path),
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
        # Each job running now, with its run: a job runs once at a time.
        self._active_runs: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._controls: dict[int, RunControl] = {}
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()

    def start_job(
        self, job: Job, trigger: str, dry_run: bool = False, step_id: int | None = None
    ) -> JobStepRunRecord | None:
        now = utc_now()
        selected_steps = _selected_steps(job, step_id)
        if job.id in self._active_runs or not selected_steps:
            status = "skipped" if job.id in self._active_runs else "success"
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

        run_stamp = _run_stamp(now)
        common_args = _common_args(job, dry_run)
        # Every step of the run sees the same ${RUN_DATE}, even across midnight.
        env = _expansion_env(job, now)
        planned_steps: list[tuple[JobStep, list[str]]] = []
        for step in selected_steps:
            try:
                planned_steps.append((step, _step_argv(step.command, common_args, env, dry_run)))
            except CommandPolicyError as exc:
                return self._record_invalid_run(job, trigger, step, run_stamp, exc)

        job_run_id = None
        try:
            first_step, first_argv = planned_steps[0]
            first_log_path = self._step_log_path(job, first_step, run_stamp)
            _create_log(first_log_path)

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
            self._active_runs[job.id] = job_run_id

            # A prune-dated step counts days from the run's ${RUN_DATE} day.
            today = _run_day(env, now)
            task = asyncio.create_task(
                self._run_job_background(job_run_id, job, planned_steps, run_stamp, today)
            )
        except BaseException:
            self._release_job(job.id, job_run_id)
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

    async def wait_for_notifications(self) -> None:
        """Wait until the notifications started so far have been sent or have failed."""
        # Finished tasks leave the set only when the loop next runs callbacks, and waiting on
        # them alone would not yield to it.
        while pending := [task for task in self._notification_tasks if not task.done()]:
            await asyncio.wait(pending)

    def _step_log_path(self, job: Job, step: JobStep, run_stamp: str) -> Path:
        return _step_log_path(self._log_root, job, step, run_stamp)

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

    async def _follow_step(
        self,
        control: RunControl,
        socket_path: Path,
        env: dict[str, str],
        step_ended: asyncio.Event,
    ) -> None:
        """Learn the step's own limit and transfers from its rclone, and keep its limit right.

        rclone's own reading of its --bwlimit and RCLONE_BWLIMIT is what it does, flag values
        and units and all. Until it has told it, nothing is sent to the step (see
        RunControl.target_rate), so rclone keeps the limit its own setting gives.

        A timetable there would put its rate back at each change of slot, ending a pause
        unseen, so the runner takes it over. rclone's ticker changes the limit only when its
        timetable's rate differs from the one it last applied, which is the limit rclone has
        now: replacing the timetable with that rate leaves it nothing to change. The runner
        then sets each slot's rate itself, unless paused or limited, and checks that rclone
        has the rate it should, in case a slot changed while it was taking over.
        """
        try:
            options = await read_rclone_options(socket_path, step_ended)
            if options is None:
                return  # The step ended first.
            transfers = options.get("Transfers")
            if isinstance(transfers, int) and transfers > 0:
                control.pause_rate = pause_rate_for(transfers)
            if not control.job_limit_known:
                control.job_limit = parse_bandwidth(options.get("BwLimit"), env)
            timetable = control.job_limit is not None and control.job_limit.is_timetable
            if timetable:
                current = await rc_call(socket_path, "core/bwlimit", {})
                await set_rclone_bwlimit_option(socket_path, str(current.get("rate", "off")))
        except (OSError, RcError) as exc:
            if not step_ended.is_set():
                logger.warning("Could not read the running rclone's bandwidth limit: %s", exc)
            return
        control.job_limit_known = True
        # Set what the step should have, if it isn't what rclone has now: the run may have
        # been paused or limited meanwhile, or the timetable moved on.
        await self._keep_rate(control, socket_path)
        while timetable and not step_ended.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(step_ended.wait(), FOLLOW_TIMETABLE_SECONDS)
            if not step_ended.is_set():
                await self._keep_rate(control, socket_path)

    async def _keep_rate(self, control: RunControl, socket_path: Path) -> None:
        """Send the step's rate to its rclone if rclone has another one."""
        target = control.target_rate()
        if target is None:
            return
        try:
            current = await rc_call(socket_path, "core/bwlimit", {})
        except OSError, RcError:
            return  # Gone, or the step ended: the next step starts with the setting.
        if not same_rate(target, current):
            await self._apply_quietly(control)

    def _start_background(self, coroutine) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

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
            # The job is done: free it for its next run now, not after the email, which a
            # slow mail server could hold up past the next scheduled run.
            self._release_job(job.id, job_run_id)
            self._notify_soon(job_run_id)

    async def _run_step(
        self, job_run_id: int, step_run: JobStepRunRecord, env: dict[str, str], today: date
    ) -> int:
        argv = json.loads(step_run.argv_json)
        # The stored argv stays the job's own command, so it can be copied and rerun as is.
        process_argv = with_dry_run_safeguards(argv, env)
        log_path = Path(step_run.log_path)
        control = self._controls.get(job_run_id)
        if is_prune_command(argv):
            # Not an rclone process with a control socket: bandwidth changes and pauses wait
            # for the next step.
            if control is not None:
                control.job_limit = None
            return await self._prune(process_argv, env, log_path, today)
        process_argv = _process_argv(process_argv, env)
        if control is None or self._control_dir is None:
            return await self._executor(process_argv, env, log_path)
        # The step's own limit, to put back after a pause or the run's limit, is what rclone
        # makes of its --bwlimit or RCLONE_BWLIMIT; it tells once it is up (_follow_step).
        # Only a step that can't have one is known to have none from the start.
        rclone_env = child_environment(env)
        control.job_limit = None
        control.job_limit_known = not may_set_bwlimit(argv, rclone_env)
        control.pause_rate = pause_rate(argv, rclone_env)
        if not supports_live_control(argv):
            # No socket to ask: for the panel's label only, read the limit from the command.
            control.job_limit = job_bandwidth(argv, rclone_env)
            control.job_limit_known = True
            return await self._executor(process_argv, env, log_path)
        socket_path = self._control_dir / f"step-{step_run.id}.sock"
        # rclone refuses to start when a socket from a killed process is still there.
        socket_path.unlink(missing_ok=True)
        spawn_argv = with_flags(process_argv, control.spawn_args(socket_path))
        control.socket_path = socket_path
        if control.target_rate(for_new_process=True) is not None:
            # A step with a limit of its own gets the run's setting only through the socket
            # (see spawn_args): send it as soon as rclone is listening.
            self._start_background(self._apply_quietly(control))
        # Stopped by this event, not cancelled: a call to rclone it is making then ends as
        # rclone does, instead of being cut off halfway.
        step_ended = asyncio.Event()
        follow = self._start_background(
            self._follow_step(control, socket_path, rclone_env, step_ended)
        )
        try:
            return await self._executor(spawn_argv, env, log_path)
        finally:
            control.socket_path = None
            step_ended.set()
            # It stops within a moment once rclone has gone; don't leave it behind.
            _done, pending = await asyncio.wait({follow}, timeout=FOLLOW_STOP_SECONDS)
            for task in pending:
                task.cancel()
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
        _create_log(log_path)
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
            stats = step_stats_from_log(json.loads(step_run.argv_json), Path(step_run.log_path))
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

    def _release_job(self, job_id: int, job_run_id: int | None) -> None:
        """Let the job run again, unless a later run of it has started meanwhile."""
        if job_run_id is not None and self._active_runs.get(job_id) == job_run_id:
            del self._active_runs[job_id]

    def _task_done(self, job_id: int, job_run_id: int, task: asyncio.Task[None]) -> None:
        self._release_job(job_id, job_run_id)
        self._tasks.pop(job_run_id, None)
        self._controls.pop(job_run_id, None)
        if not task.cancelled():
            task.exception()


async def _send_latest_rate(control: RunControl, socket_path: Path) -> None:
    """Send limit changes to one rclone until it has the latest, one call at a time."""
    sent = None
    while control.socket_path == socket_path and (rate := control.target_rate()) != sent:
        if rate is None:
            return  # Nothing to set until rclone has told the step's own limit.
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


def _run_stamp(value: datetime) -> str:
    """A name for one run's logs: its start time, then a random part, so two runs of a job
    started at the same moment still write and delete separate logs."""
    return f"{value.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(4)}"


def _process_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """The command to start for a step: its own, asking rclone to log its transfer totals.

    The flags go right after the subcommand, so a --stats-log-level or --stats of the job's own
    comes later and wins; one set in the environment is left alone, since the flag would beat
    it.
    """
    if len(argv) < 2 or argv[1] not in STATS_SUBCOMMANDS:
        return argv
    rclone_env = child_environment(env)
    flags = [flag for flag, variable in STATS_FLAGS.items() if variable not in rclone_env]
    return with_flags(argv, flags) if flags else argv


def _step_log_path(log_root: Path, job: Job, step: JobStep, run_stamp: str) -> Path:
    return log_root / f"job-{job.id}" / f"{run_stamp}-step-{step.id}.log"


def _create_log(path: Path) -> None:
    """Create a new, empty step log; never reuse another run's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)


def _expansion_env(job: Job, started_at: datetime) -> dict[str, str]:
    return run_variables(started_at, settings.timezone) | job.env


def _run_day(env: dict[str, str], started_at: datetime) -> date:
    """The day prune-dated counts from: the run's ${RUN_DATE}. A job whose own RUN_DATE is not
    a date can't have prune-dated steps (_step_argv refuses them), so then nothing uses it."""
    try:
        return effective_run_date(env)
    except CommandPolicyError:
        return run_date(started_at, settings.timezone)


def _step_argv(command: str, common_args: str, env: dict[str, str], dry_run: bool) -> list[str]:
    argv = build_step_argv(command, common_args, env)
    if is_prune_command(argv):
        effective_run_date(env)  # Refuses a ${RUN_DATE} that isn't a date.
    # A dry run asked for in the command or its environment counts too: it is still meant to
    # change nothing.
    if dry_run or requests_dry_run(argv, env):
        refusal = dry_run_refusal(argv, env)
        if refusal is not None:
            raise CommandPolicyError(refusal)
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

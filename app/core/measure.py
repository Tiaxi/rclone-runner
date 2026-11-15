"""Run ``rclone size`` for step sources in the background, one at a time."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.core.aio import run_to_completion
from app.core.commands import child_environment
from app.core.models import utc_now
from app.core.sources import SizeCommand, parse_size_output
from app.db import SourceMeasurementRecord

logger = logging.getLogger(__name__)

# A large tree on slow storage can take a while; this only stops a stuck rclone.
MEASURE_TIMEOUT_SECONDS = 2 * 60 * 60
TERMINATE_TIMEOUT_SECONDS = 5
ERROR_MAX_CHARS = 500


class SourceMeasurer:
    def __init__(self, session_factory, timeout: float = MEASURE_TIMEOUT_SECONDS) -> None:
        self._session_factory = session_factory
        self._timeout = timeout
        # One measurement at a time: each walks a whole tree, which is hard on the disks.
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, commands: list[SizeCommand], env: dict[str, str]) -> int:
        """Queue measurements, skipping sources already being measured. Returns how many."""
        queued = []
        now = utc_now()
        with self._session_factory() as db:
            for command in dict((command.key, command) for command in commands).values():
                record = db.query(SourceMeasurementRecord).filter_by(key=command.key).first()
                if record is not None and record.status == "running":
                    continue
                if record is None:
                    record = SourceMeasurementRecord(key=command.key, source=command.source)
                    db.add(record)
                record.status = "running"
                record.started_at = now
                record.error = None
                queued.append(command)
            db.commit()
        for command in queued:
            task = asyncio.create_task(self._measure(command, env))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(queued)

    def recover_interrupted(self) -> None:
        """Mark measurements a previous process left running as failed. Call at startup."""
        with self._session_factory() as db:
            for record in db.query(SourceMeasurementRecord).filter_by(status="running"):
                record.status = "failed"
                record.error = "Interrupted: the app restarted while measuring."
            db.commit()

    async def wait(self) -> None:
        """Wait for queued measurements to finish (for tests)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _measure(self, command: SizeCommand, env: dict[str, str]) -> None:
        try:
            async with self._lock:
                result, error = await self._run(command, env)
        except asyncio.CancelledError:
            self._finish(command, None, "Stopped: the app shut down while measuring.")
            raise
        except Exception as exc:
            logger.exception("Measuring %s failed", command.source)
            result, error = None, f"Could not run rclone: {exc}"
        self._finish(command, result, error)

    async def _run(self, command: SizeCommand, env: dict[str, str]):
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment(env),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            await run_to_completion(_stop(process))
            return None, f"Timed out after {self._timeout / 3600:g} h."
        except BaseException:
            await run_to_completion(_stop(process))
            raise
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip().splitlines()
            detail = message[-1] if message else f"exit code {process.returncode}"
            return None, detail[-ERROR_MAX_CHARS:]
        try:
            return parse_size_output(stdout.decode()), None
        except ValueError, KeyError, TypeError:
            return None, "rclone size returned output that could not be read."

    def _finish(self, command: SizeCommand, result, error: str | None) -> None:
        with self._session_factory() as db:
            record = db.query(SourceMeasurementRecord).filter_by(key=command.key).first()
            if record is None:
                return
            if result is not None:
                record.status = "success"
                record.bytes = result.bytes
                record.files = result.files
                record.measured_at = utc_now()
                record.error = None
            else:
                record.status = "failed"
                record.error = error
            db.commit()


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT_SECONDS)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()

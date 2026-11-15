from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress


async def run_to_completion(cleanup: Awaitable[None]) -> bool:
    """Await cleanup until it finishes, even if the caller is cancelled again meanwhile.

    A second cancellation (for example a repeated Cancel click) would otherwise interrupt the
    cleanup halfway, such as between SIGTERM and SIGKILL, and leave a process running.
    Returns True if a cancellation arrived while waiting; the caller should then re-raise
    CancelledError once it has finished its own cleanup.
    """
    task = asyncio.ensure_future(cleanup)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            return cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def stop_process(process: asyncio.subprocess.Process, timeout: float) -> None:
    """Ask a process to stop, and kill it if it has not stopped after timeout seconds."""
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()

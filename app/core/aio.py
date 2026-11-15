from __future__ import annotations

import asyncio
from collections.abc import Awaitable


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

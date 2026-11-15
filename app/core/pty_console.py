from __future__ import annotations

import asyncio
import os
import pty
import shlex
import signal
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.core.commands import child_environment

DRAIN_TIMEOUT_SECONDS = 5.0
TERMINATE_TIMEOUT_SECONDS = 5.0


async def bridge_pty(
    argv: list[str],
    receive_text: Callable[[], Awaitable[str]],
    send_text: Callable[[str], Awaitable[None]],
) -> int:
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=child_environment(),
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    os.set_blocking(master_fd, False)
    reader_task = asyncio.create_task(_read_pty(master_fd, send_text))
    writer_task = asyncio.create_task(_write_pty(master_fd, process.pid, receive_text))
    try:
        while process.poll() is None:
            if writer_task.done():
                signal_exit_code = writer_task.result()
                # None means the input side went away (disconnect).
                return 130 if signal_exit_code is None else signal_exit_code
            await asyncio.sleep(0.1)
        # The process has exited; let the reader send what is still buffered in the pty.
        with suppress(TimeoutError):
            await asyncio.wait_for(reader_task, timeout=DRAIN_TIMEOUT_SECONDS)
        return _display_exit_code(process.returncode)
    finally:
        writer_task.cancel()
        reader_task.cancel()
        await _stop_process_group(process)
        os.close(master_fd)


async def _stop_process_group(process: subprocess.Popen) -> None:
    """Terminate the command if it is still running, and always reap it."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TERMINATE_TIMEOUT_SECONDS
    while process.poll() is None and loop.time() < deadline:
        await asyncio.sleep(0.05)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await asyncio.to_thread(process.wait)


async def _read_pty(
    master_fd: int,
    send_text: Callable[[str], Awaitable[None]],
) -> None:
    while True:
        try:
            data = os.read(master_fd, 4096)
        except BlockingIOError:
            await asyncio.sleep(0.02)
            continue
        except OSError:
            return
        if not data:
            return
        await send_text(data.decode(errors="replace"))


async def _write_pty(
    master_fd: int,
    process_pid: int,
    receive_text: Callable[[], Awaitable[str]],
) -> int | None:
    while True:
        try:
            text = await receive_text()
            if "\x03" in text:
                with suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGINT)
                return 130
            if "\x04" in text:
                with suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGHUP)
                return 129
            await _write_all(master_fd, text.encode())
        except RuntimeError, OSError, asyncio.CancelledError:
            return None


async def _write_all(fd: int, data: bytes) -> None:
    """Write everything to a non-blocking fd, waiting while the pty input buffer is full."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            await asyncio.sleep(0.01)
            continue
        view = view[written:]


def display_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _display_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 0
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode

from __future__ import annotations

import asyncio
import os
import pty
import shlex
import signal
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress


async def bridge_pty(
    argv: list[str],
    receive_text: Callable[[], Awaitable[str]],
    send_text: Callable[[str], Awaitable[None]],
) -> int:
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(  # noqa: S603
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    loop = asyncio.get_running_loop()
    reader_task = asyncio.create_task(_read_pty(loop, master_fd, send_text))
    writer_task = asyncio.create_task(_write_pty(master_fd, process.pid, receive_text))
    disconnected = False
    try:
        while process.poll() is None:
            if writer_task.done():
                signal_exit_code = writer_task.result()
                if signal_exit_code is not None:
                    return signal_exit_code
                disconnected = True
                break
            await asyncio.sleep(0.1)
        if disconnected:
            return 130
        return _display_exit_code(process.returncode)
    finally:
        writer_task.cancel()
        reader_task.cancel()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        os.close(master_fd)


async def _read_pty(
    loop: asyncio.AbstractEventLoop,
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
            os.write(master_fd, text.encode())
        except RuntimeError, OSError, asyncio.CancelledError:
            return None


def display_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _display_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 0
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode

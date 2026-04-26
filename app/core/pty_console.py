from __future__ import annotations

import asyncio
import os
import pty
import shlex
import signal
import subprocess
from collections.abc import Awaitable, Callable


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
    loop = asyncio.get_running_loop()
    reader_task = asyncio.create_task(_read_pty(loop, master_fd, send_text))
    writer_task = asyncio.create_task(_write_pty(master_fd, receive_text))
    disconnected = False
    try:
        while process.poll() is None:
            if writer_task.done():
                disconnected = True
                break
            await asyncio.sleep(0.1)
        if disconnected:
            return 130
        return process.returncode or 0
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
            data = await loop.run_in_executor(None, os.read, master_fd, 4096)
        except OSError:
            return
        if not data:
            return
        await send_text(data.decode(errors="replace"))


async def _write_pty(master_fd: int, receive_text: Callable[[], Awaitable[str]]) -> None:
    while True:
        try:
            text = await receive_text()
            os.write(master_fd, text.encode())
        except RuntimeError, OSError, asyncio.CancelledError:
            return


def display_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)

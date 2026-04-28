import asyncio
import sys

from app.core.pty_console import bridge_pty


async def test_bridge_pty_ctrl_c_interrupts_running_process():
    queue: asyncio.Queue[str] = asyncio.Queue()
    output = []

    async def receive_text() -> str:
        return await queue.get()

    async def send_text(text: str) -> None:
        output.append(text)

    async def interrupt() -> None:
        await asyncio.sleep(0.1)
        await queue.put("\x03")

    interrupt_task = asyncio.create_task(interrupt())
    try:
        exit_code = await asyncio.wait_for(
            bridge_pty(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                receive_text,
                send_text,
            ),
            timeout=2,
        )
    finally:
        interrupt_task.cancel()

    assert exit_code == 130


async def test_bridge_pty_ctrl_d_stops_running_process():
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def receive_text() -> str:
        return await queue.get()

    async def send_text(text: str) -> None:
        pass

    async def send_eof() -> None:
        await asyncio.sleep(0.1)
        await queue.put("\x04")

    eof_task = asyncio.create_task(send_eof())
    try:
        exit_code = await asyncio.wait_for(
            bridge_pty(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                receive_text,
                send_text,
            ),
            timeout=2,
        )
    finally:
        eof_task.cancel()

    assert exit_code == 129

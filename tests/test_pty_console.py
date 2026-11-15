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


async def _run_bridge(argv, inputs=(), send_delay=0.0):
    queue: asyncio.Queue[str] = asyncio.Queue()
    for item in inputs:
        queue.put_nowait(item)
    output = []

    async def send_text(text: str) -> None:
        output.append(text)
        await asyncio.sleep(send_delay)

    exit_code = await asyncio.wait_for(bridge_pty(argv, queue.get, send_text), timeout=30)
    return exit_code, "".join(output)


async def test_bridge_pty_sends_output_written_just_before_exit():
    script = "import sys; sys.stdout.write('line\\n' * 5000 + 'DONE'); sys.stdout.flush()"

    # A slow client leaves output queued in the pty when the command exits.
    exit_code, output = await _run_bridge([sys.executable, "-c", script], send_delay=0.02)

    assert exit_code == 0
    assert output.count("line") == 5000
    assert output.endswith("DONE")


async def test_bridge_pty_writes_input_larger_than_pty_buffer():
    script = (
        "import sys, time\n"
        "time.sleep(0.3)\n"
        "count = sum(1 for _ in range(1000) if sys.stdin.readline())\n"
        "print('received', count)\n"
    )
    paste = ("y" * 100 + "\n") * 1000

    exit_code, output = await _run_bridge([sys.executable, "-c", script], inputs=[paste])

    assert exit_code == 0
    assert "received 1000" in output

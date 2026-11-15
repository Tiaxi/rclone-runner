import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

import app.core.pty_console as pty_module
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


async def test_bridge_pty_hides_app_settings_from_console_commands(monkeypatch):
    monkeypatch.setenv("RCLONE_RUNNER_SECRET_KEY", "session-secret")
    script = "import os; print('secret' if 'RCLONE_RUNNER_SECRET_KEY' in os.environ else 'clean')"

    exit_code, output = await _run_bridge([sys.executable, "-c", script])

    assert exit_code == 0
    assert "clean" in output


SIGTERM_IGNORING_CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "while True:\n"
    "    print('tick', flush=True); time.sleep(0.05)\n"
)


async def _wait_for_pid(pid_path: Path) -> int:
    for _ in range(200):
        if pid_path.exists() and pid_path.read_text():
            return int(pid_path.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("child did not start")


def _assert_reaped(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"process {pid} was still running")


async def test_bridge_pty_finishes_shutdown_when_cancelled_twice(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pty_module, "TERMINATE_TIMEOUT_SECONDS", 0.5)
    pid_path = tmp_path / "child.pid"
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def send_text(text: str) -> None:
        pass

    task = asyncio.create_task(
        bridge_pty(
            [sys.executable, "-c", SIGTERM_IGNORING_CHILD, str(pid_path)], queue.get, send_text
        )
    )
    pid = await _wait_for_pid(pid_path)

    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    _assert_reaped(pid)


async def test_bridge_pty_stops_command_when_output_cannot_be_delivered(tmp_path: Path):
    pid_path = tmp_path / "child.pid"
    script = (
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    print('x' * 1000, flush=True); time.sleep(0.01)\n"
    )
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def send_text(text: str) -> None:
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError, match="No space left"):
        await asyncio.wait_for(
            bridge_pty([sys.executable, "-c", script, str(pid_path)], queue.get, send_text),
            timeout=10,
        )

    _assert_reaped(int(pid_path.read_text()))

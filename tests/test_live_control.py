import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.live_control as live_control
import app.core.runner as runner_module
from app.core.live_control import (
    RateError,
    RcError,
    RunControl,
    configured_rate,
    normalize_rate,
    pause_rate,
    rate_label,
    rc_call,
    supports_live_control,
)
from app.core.models import Job, JobStep
from app.core.runner import LiveJobRunner
from app.db import Base, JobStepRunRecord


@pytest.fixture
def control_dir():
    # Unix socket paths are limited to about 100 characters, so keep this one short.
    path = Path(tempfile.mkdtemp(prefix="rr-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_normalize_rate_accepts_what_rclone_accepts():
    assert normalize_rate(" 8m ") == "8M"
    assert normalize_rate("750k") == "750K"
    assert normalize_rate("1.5M") == "1.5M"
    assert normalize_rate("OFF") == "off"
    assert normalize_rate("3m:off") == "3M:off"
    assert normalize_rate("512") == "512"
    for bad in ("fast", "8 Mbit", "", "1M:2M:3M", "08:00,512k 23:00,off", "-1M"):
        with pytest.raises(RateError):
            normalize_rate(bad)


def test_rate_labels_spell_out_units():
    assert rate_label("off") == "Unlimited"
    assert rate_label("10M") == "10 MiB/s"
    assert rate_label("512K") == "512 KiB/s"
    assert rate_label("512") == "512 KiB/s"
    assert rate_label("3M:off") == "3 MiB/s up, Unlimited down"
    assert rate_label(None) == "Not limited"


def test_configured_rate_reads_a_single_bwlimit_value():
    assert configured_rate(["rclone", "sync", "--bwlimit", "2M", "a", "b"]) == "2M"
    assert configured_rate(["rclone", "sync", "--bwlimit=1.5m", "a", "b"]) == "1.5M"
    assert configured_rate(["rclone", "sync", "a", "b"]) is None
    # A timetable has no single value to go back to.
    assert configured_rate(["rclone", "sync", "--bwlimit", "08:00,512k 23:00,off"]) is None


def test_pause_rate_allows_a_little_per_parallel_transfer():
    assert pause_rate(["rclone", "sync", "a", "b"]) == "64K"
    assert pause_rate(["rclone", "sync", "--transfers", "20", "a", "b"]) == "320K"
    assert pause_rate(["rclone", "sync", "--transfers=1", "a", "b"]) == "16K"
    assert pause_rate(["rclone", "sync", "--transfers=0", "a", "b"]) == "16K"


def test_live_control_skips_commands_with_their_own_remote_control():
    assert supports_live_control(["rclone", "sync", "a", "b"])
    assert not supports_live_control(["rclone", "sync", "--rc", "a", "b"])
    assert not supports_live_control(["rclone", "sync", "--rc-addr=:5572", "a", "b"])
    assert not supports_live_control(["rclone", "rc", "core/stats"])
    assert not supports_live_control(["rclone", "rcd"])


def test_run_control_target_rates():
    control = RunControl(configured="2M")
    assert control.target_rate() == "2M"
    assert control.target_rate(for_new_process=True) is None
    control.limit = "8M"
    assert control.target_rate() == "8M"
    control.paused = True
    assert control.target_rate() == "64K"
    assert control.spawn_args(Path("/s.sock"))[-2:] == ["--bwlimit", "64K"]
    control.paused = False
    control.limit = None
    control.configured = None
    assert control.target_rate() == "off"
    assert "--bwlimit" not in control.spawn_args(Path("/s.sock"))


async def _fake_rc_server(
    socket_path: Path, received: list, *, status=200, chunked=False, delay=0.0
):
    """A stand-in for rclone's remote control API that records each call."""

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        length = next(
            int(line.split(":", 1)[1])
            for line in lines
            if line.lower().startswith("content-length")
        )
        params = json.loads(await reader.readexactly(length))
        received.append((lines[0].split()[1], params))
        # rclone answers only once its transfers' current reads pass the old limit.
        await asyncio.sleep(delay)
        reply = {"rate": params.get("rate", "2Mi")} if status == 200 else {"error": "bad bwlimit"}
        payload = json.dumps(reply).encode()
        if chunked:
            body = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
            headers = b"Transfer-Encoding: chunked\r\n"
        else:
            body = payload
            headers = f"Content-Length: {len(payload)}\r\n".encode()
        writer.write(f"HTTP/1.1 {status} X\r\n".encode() + headers + b"\r\n" + body)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(socket_path))


async def test_rc_call_reads_plain_and_chunked_replies_and_errors(control_dir):
    received = []
    for chunked in (False, True):
        socket_path = control_dir / f"rc-{chunked}.sock"
        server = await _fake_rc_server(socket_path, received, chunked=chunked)
        async with server:
            assert await rc_call(socket_path, "core/bwlimit", {"rate": "1K"}) == {"rate": "1K"}
    assert received == [("/core/bwlimit", {"rate": "1K"})] * 2

    socket_path = control_dir / "rc-error.sock"
    async with await _fake_rc_server(socket_path, received, status=500):
        with pytest.raises(RcError, match="bad bwlimit"):
            await rc_call(socket_path, "core/bwlimit", {"rate": "fast"})


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class FakeRclone:
    """An executor that serves the remote control socket from its arguments, like rclone."""

    def __init__(self, serve: bool = True, delay: float = 0.0):
        self.serve = serve
        self.delay = delay
        self.calls: list[list[str]] = []
        self.received: list[tuple[str, dict]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, argv, env, log_path):
        self.calls.append(argv)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        server = None
        if self.serve and "--rc-addr" in argv:
            socket_path = Path(argv[argv.index("--rc-addr") + 1].removeprefix("unix://"))
            assert not socket_path.exists(), "a stale socket would stop rclone from starting"
            server = await _fake_rc_server(socket_path, self.received, delay=self.delay)
        self.started.set()
        await self.release.wait()
        self.release.clear()
        self.started.clear()
        if server is not None:
            server.close()
            await server.wait_closed()
        return 0


def _job(common_args="", commands=("sync /a remote:a",)):
    steps = [
        JobStep(id=index, name=f"step-{index}", command=command)
        for index, command in enumerate(commands, start=1)
    ]
    return Job(id=1, name="backup", common_args=common_args, env={}, steps=steps)


async def test_runner_adds_control_flags_without_storing_them(control_dir, tmp_path):
    rclone = FakeRclone()
    session_factory = _session_factory(tmp_path / "runs.db")
    runner = LiveJobRunner(
        tmp_path / "logs", session_factory, executor=rclone, control_dir=control_dir
    )
    # A socket left behind by a killed rclone must not stop the next one from starting.
    (control_dir / "step-1.sock").touch()

    first = runner.start_job(_job(), trigger="manual")
    await rclone.started.wait()

    argv = rclone.calls[0]
    socket_arg = f"unix://{control_dir / 'step-1.sock'}"
    assert argv[-4:] == ["--rc", "--rc-addr", socket_arg, "--rc-no-auth"]
    with session_factory() as db:
        stored = json.loads(db.get(JobStepRunRecord, first.id).argv_json)
    assert stored == ["rclone", "sync", "/a", "remote:a"]
    assert runner.control_state(first.job_run_id) == {
        "available": True,
        "paused": False,
        "limit": None,
        "limit_label": None,
        "label": "Not limited",
        "pause_label": "64 KiB/s",
        "source": "job",
    }

    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)
    assert runner.control_state(first.job_run_id) is None
    assert not (control_dir / "step-1.sock").exists()


async def test_runner_changes_the_running_step_and_carries_settings_forward(control_dir, tmp_path):
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(
        _job("--bwlimit 2M", ("sync /a remote:a", "sync /b remote:b")), trigger="manual"
    )
    job_run_id = first.job_run_id
    await rclone.started.wait()

    await runner.set_bandwidth(job_run_id, "8m")
    await runner.pause(job_run_id)
    assert [params["rate"] for _, params in rclone.received] == ["8M", "64K"]
    assert runner.control_state(job_run_id)["label"] == "Paused"

    # The next step starts paused, through --bwlimit after the job's own one.
    rclone.release.set()
    while len(rclone.calls) < 2:
        await asyncio.sleep(0.01)
    await rclone.started.wait()
    assert rclone.calls[1][-2:] == ["--bwlimit", "64K"]

    await runner.resume(job_run_id)
    await runner.set_bandwidth(job_run_id, None)
    # Resuming restores the run's limit; clearing it goes back to the job's --bwlimit.
    assert [params["rate"] for _, params in rclone.received[2:]] == ["8M", "2M"]
    assert runner.control_state(job_run_id)["label"] == "2 MiB/s"

    rclone.release.set()
    await runner.wait_for_job_run(job_run_id)


async def test_runner_leaves_jobs_with_their_own_remote_control_alone(control_dir, tmp_path):
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job("--rc --rc-addr :5572"), trigger="manual")
    await rclone.started.wait()

    assert "--rc-no-auth" not in rclone.calls[0]
    assert runner.control_state(first.job_run_id)["available"] is False
    # Nothing to call now; the choice is kept for any later step.
    await runner.pause(first.job_run_id)
    assert rclone.received == []

    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_runner_reports_an_unreachable_rclone(control_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(live_control, "CONNECT_RETRY_SECONDS", 0.001)
    rclone = FakeRclone(serve=False)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job(), trigger="manual")
    await rclone.started.wait()

    with pytest.raises(RcError, match="could not reach"):
        await runner.pause(first.job_run_id)
    # The pause still applies to later steps.
    assert runner.control_state(first.job_run_id)["paused"] is True

    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_runner_rejects_bad_rates_and_finished_runs(control_dir, tmp_path):
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job(), trigger="manual")
    await rclone.started.wait()

    with pytest.raises(RateError):
        await runner.set_bandwidth(first.job_run_id, "fast")
    assert rclone.received == []

    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)
    with pytest.raises(KeyError):
        await runner.pause(first.job_run_id)


async def test_slow_rclone_replies_do_not_fail_and_the_last_change_wins(
    control_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(runner_module, "APPLY_WAIT_SECONDS", 0.05)
    rclone = FakeRclone(delay=0.2)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job("--transfers=2"), trigger="manual")
    await rclone.started.wait()

    # Both return before rclone answers; the second waits for the first to be sent.
    await runner.pause(first.job_run_id)
    await runner.resume(first.job_run_id)
    while len(rclone.received) < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)

    assert [params["rate"] for _, params in rclone.received] == ["32K", "off"]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_real_rclone_accepts_the_control_flags(control_dir, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"x" * 4 * 1024 * 1024)
    socket_path = control_dir / "real.sock"
    process = await asyncio.create_subprocess_exec(
        "rclone",
        "copy",
        str(source),
        str(tmp_path / "dest"),
        "--bwlimit",
        "100K",
        *RunControl().spawn_args(socket_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await live_control.set_rclone_bandwidth(socket_path, "64K")
        reply = await rc_call(socket_path, "core/bwlimit", {})
        assert reply["rate"] == "64Ki"
    finally:
        process.kill()
        await process.wait()

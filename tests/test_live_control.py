import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime
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
    job_bandwidth,
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
    # The last two: a Kelvin sign for K, and Arabic-Indic digits, which rclone rejects.
    for bad in (
        "fast",
        "8 Mbit",
        "",
        "1M:2M:3M",
        "08:00,512k 23:00,off",
        "-1M",
        "1\u212a",
        "\u0661K",
    ):
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
    control = RunControl(
        job_limit=job_bandwidth(["rclone", "sync", "--bwlimit", "2M"], {}), job_limit_known=True
    )
    assert control.target_rate() == "2M"
    assert control.target_rate(for_new_process=True) is None
    control.limit = "8M"
    assert control.target_rate() == "8M"
    control.paused = True
    assert control.target_rate() == "64K"
    # The job's own limit beats or replaces one on the command line: it goes by the socket.
    assert not any(arg.startswith("--bwlimit") for arg in control.spawn_args(Path("/s.sock")))
    control.job_limit = None
    assert control.spawn_args(Path("/s.sock"))[-1] == "--bwlimit=64K"
    control.paused = False
    control.limit = None
    assert control.target_rate() == "off"
    assert not any(arg.startswith("--bwlimit") for arg in control.spawn_args(Path("/s.sock")))


async def _fake_rc_server(
    socket_path: Path,
    received: list,
    *,
    status=200,
    chunked=False,
    delay=0.0,
    options: dict | None = None,
    option_calls: list | None = None,
    state: dict | None = None,
):
    """A stand-in for rclone's remote control API that records each core/bwlimit call, and
    answers options/get and options/set from options, recording those in option_calls.

    With state, it keeps the limit there ("rate") like rclone: a core/bwlimit call without a
    rate reads it, and isn't recorded.
    """

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        length = next(
            int(line.split(":", 1)[1])
            for line in lines
            if line.lower().startswith("content-length")
        )
        params = json.loads(await reader.readexactly(length))
        path = lines[0].split()[1]
        if path.startswith("/options/"):
            if option_calls is not None:
                option_calls.append((path, params))
            if path == "/options/set" and options is not None:
                options.update(params.get("main", {}))
            reply = {"main": dict(options or {})} if path == "/options/get" else {}
        elif state is not None and "rate" not in params:
            reply = _bwlimit_reply(state["rate"])
        else:
            if state is not None:
                state["rate"] = params["rate"]
            received.append((path, params))
            # rclone answers only once its transfers' current reads pass the old limit.
            await asyncio.sleep(delay)
            reply = (
                {"rate": params.get("rate", "2Mi")} if status == 200 else {"error": "bad bwlimit"}
            )
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


class _Both:
    """Record each call in two lists."""

    def __init__(self, *lists):
        self.lists = lists

    def append(self, item):
        for target in self.lists:
            target.append(item)


def _bwlimit_reply(rate):
    """rclone's core/bwlimit reply for a rate it has."""
    upload, _, download = rate.partition(":")
    sides = [live_control.rate_bytes_per_second(side) for side in (upload, download or upload)]
    tx, rx = (-1 if side is None else int(side) for side in sides)
    return {"rate": rate, "bytesPerSecondTx": tx, "bytesPerSecondRx": rx, "bytesPerSecond": tx}


def _fake_bwlimit(argv, env):
    """The --bwlimit option as rclone would read it: the command's last, else the
    environment's (the container's, then the job's)."""
    value = None
    for index, arg in enumerate(argv):
        if arg == "--bwlimit" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--bwlimit="):
            value = arg.split("=", 1)[1]
    return value or (os.environ | env).get("RCLONE_BWLIMIT", "off")


class FakeRclone:
    """An executor that serves the remote control socket from its arguments, like rclone."""

    def tick(self):
        """rclone's timetable ticker: when its --bwlimit option gives another rate than the one
        it applied last, it sets that rate, whatever the remote control set meanwhile."""
        own = live_control.parse_bandwidth(self.options["BwLimit"], self.env)
        now = own.rate_now() if own is not None else "off"
        if now != self.applied:
            self.applied = now
            self.state["rate"] = now

    def __init__(
        self,
        serve: bool = True,
        delay: float = 0.0,
        bwlimit: str | None = None,
        start_delay: float = 0.0,
    ):
        self.serve = serve
        self.delay = delay
        # How long it takes to start listening.
        self.start_delay = start_delay
        # What rclone answers for its --bwlimit option, when not what _fake_bwlimit reads.
        self.bwlimit = bwlimit
        self.calls: list[list[str]] = []
        self.received: list[tuple[str, dict]] = []
        # options/get and options/set calls, and the options rclone read at its start.
        self.option_calls: list[tuple[str, dict]] = []
        self.options: dict = {}
        self.received_by_socket: dict[str, list] = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, argv, env, log_path):
        self.calls.append(argv)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        server = None
        addr = next((arg for arg in argv if arg.startswith("--rc-addr=unix://")), None)
        if self.serve and addr is not None:
            socket_path = Path(addr.removeprefix("--rc-addr=unix://"))
            assert not socket_path.exists(), "a stale socket would stop rclone from starting"
            calls = self.received_by_socket.setdefault(socket_path.name, [])
            self.options = {"BwLimit": self.bwlimit or _fake_bwlimit(argv, env), "Transfers": 4}
            # The limit rclone starts with: the one its option gives now.
            self.env = env
            own = live_control.parse_bandwidth(self.options["BwLimit"], env)
            self.state = {"rate": own.rate_now() if own is not None else "off"}
            self.applied = self.state["rate"]
            await asyncio.sleep(self.start_delay)
            server = await _fake_rc_server(
                socket_path,
                _Both(self.received, calls),
                delay=self.delay,
                options=self.options,
                option_calls=self.option_calls,
                state=self.state,
            )
        self.started.set()
        await self.release.wait()
        self.release.clear()
        self.started.clear()
        if server is not None:
            # Take new connections no more, and let one already arriving be answered, before
            # closing: asyncio's server mishandles a connection accepted as it closes.
            socket_path.unlink(missing_ok=True)
            await asyncio.sleep(0.05)
            server.close()
            await server.wait_closed()
        return 0


async def _known(runner, job_run_id):
    """Wait until the running step's rclone has told the runner its own limit."""
    control = runner._controls[job_run_id]
    while not (control.job_limit_known and control.socket_path):
        await asyncio.sleep(0.01)


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
    assert argv[2:5] == ["--rc", f"--rc-addr={socket_arg}", "--rc-no-auth"]
    with session_factory() as db:
        stored = json.loads(db.get(JobStepRunRecord, first.id).argv_json)
    assert stored == ["rclone", "sync", "/a", "remote:a"]
    assert runner.control_state(first.job_run_id) == {
        "available": True,
        "paused": False,
        "paused_now": False,
        "limit": None,
        "limit_label": None,
        "label": "Not limited",
        "detail": "The job sets no limit",
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
    await _known(runner, job_run_id)

    await runner.set_bandwidth(job_run_id, "8m")
    await runner.pause(job_run_id)
    assert [params["rate"] for _, params in rclone.received] == ["8M", "64K"]
    assert runner.control_state(job_run_id)["label"] == "Paused"

    # The next step starts paused. The job's own --bwlimit would win on the command line, so
    # the runner sends the pause once rclone is up.
    rclone.release.set()
    while len(rclone.calls) < 2:
        await asyncio.sleep(0.01)
    await rclone.started.wait()
    assert "--bwlimit=64K" not in rclone.calls[1]
    while len(rclone.received) < 3:
        await asyncio.sleep(0.01)
    assert rclone.received[2][1]["rate"] == "64K"

    await runner.resume(job_run_id)
    await runner.set_bandwidth(job_run_id, None)
    # Resuming restores the run's limit; clearing it goes back to the job's --bwlimit.
    assert [params["rate"] for _, params in rclone.received[3:]] == ["8M", "2M"]
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


async def test_a_change_after_a_step_ends_reaches_the_next_steps_rclone(
    control_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(runner_module, "APPLY_WAIT_SECONDS", 0.01)
    rclone = FakeRclone(delay=0.3)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job(commands=("sync /a r:a", "sync /b r:b")), trigger="manual")
    await rclone.started.wait()
    await runner.pause(first.job_run_id)  # still waiting for step 1's slow reply

    rclone.release.set()
    while len(rclone.calls) < 2:
        await asyncio.sleep(0.01)
    await rclone.started.wait()
    await runner.set_bandwidth(first.job_run_id, "8M")
    await runner.resume(first.job_run_id)
    await asyncio.sleep(0.8)

    step_two = rclone.received_by_socket[f"step-{first.id + 1}.sock"]
    assert step_two[-1][1]["rate"] == "8M"
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


@pytest.mark.parametrize(
    "command",
    [
        "copy -- /source remote:dest",
        # Here "--" is the backup folder's name, not the end of the options.
        "copy /source remote:dest --backup-dir -- -vv",
    ],
)
async def test_control_flags_go_right_after_the_subcommand(control_dir, tmp_path, command):
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job(commands=(command,)), trigger="manual")
    await rclone.started.wait()

    argv = rclone.calls[0]
    assert argv[:3] == ["rclone", "copy", "--rc"]
    # The job's own arguments follow unchanged.
    assert argv[5:] == command.split()[1:]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_a_pause_on_an_uncontrolled_step_is_shown_as_pending(control_dir, tmp_path):
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job("--rc --bwlimit 2M"), trigger="manual")
    await rclone.started.wait()

    await runner.pause(first.job_run_id)
    state = runner.control_state(first.job_run_id)
    assert state["paused"] is True
    assert state["paused_now"] is False
    assert state["label"] == "2 MiB/s (pause pending)"
    assert state["detail"] == "Takes effect when the next step starts"

    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_real_rclone_accepts_the_control_flags(control_dir, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"x" * 4 * 1024 * 1024)
    socket_path = control_dir / "real.sock"
    control = RunControl(paused=True)
    job_argv = ["rclone", "copy", str(source), str(tmp_path / "dest"), "--bwlimit", "100K"]
    process = await asyncio.create_subprocess_exec(
        *live_control.with_flags(job_argv, control.spawn_args(socket_path)),
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


def test_job_bandwidth_reads_the_command_then_the_environment():
    sync = ["rclone", "sync", "a", "b"]
    from_env = job_bandwidth(sync, {"RCLONE_BWLIMIT": "8M"})
    assert from_env.value == "8M"
    # A --bwlimit in the command wins over the environment, as in rclone.
    assert job_bandwidth([*sync, "--bwlimit=2m"], {"RCLONE_BWLIMIT": "8M"}).value == "2m"
    assert job_bandwidth(sync, {}) is None
    assert job_bandwidth(sync, {"RCLONE_BWLIMIT": "fast"}) is None


def test_job_bandwidth_follows_timetables():
    limit = job_bandwidth(["rclone", "sync", "--bwlimit", "08:00,512k 12:00,10M 23:00,off"], {})
    assert limit.is_timetable
    monday = datetime(2026, 11, 16)
    assert limit.rate_at(monday.replace(hour=7, minute=59)) == "off"  # From 23:00 the day before.
    assert limit.rate_at(monday.replace(hour=8)) == "512k"
    assert limit.rate_at(monday.replace(hour=12, minute=30)) == "10M"
    assert limit.rate_at(monday.replace(hour=23, minute=1)) == "off"

    weekly = job_bandwidth(
        ["rclone", "sync", "a", "b"],
        {"RCLONE_BWLIMIT": "Mon-08:00,512 Fri-23:59,10M:1M Sat-10:00,1M Sunday-20:00,off"},
    )
    assert weekly.rate_at(datetime(2026, 11, 18, 12)) == "512"  # Wednesday
    assert weekly.rate_at(datetime(2026, 11, 21, 9)) == "10M:1M"  # Saturday morning
    assert weekly.rate_at(datetime(2026, 11, 22, 21)) == "off"  # Sunday evening
    # Early Monday is still Sunday evening's rate: the week wraps around.
    assert weekly.rate_at(datetime(2026, 11, 16, 7, 59)) == "off"

    for bad in ("8:00,1M 12:00,2M", "08:00,1M Funday-12:00,2M", "25:00,1M", "08:00,lots"):
        assert job_bandwidth(["rclone", "sync", "--bwlimit", bad], {}) is None


def test_pause_rate_reads_transfers_from_the_environment_too():
    assert pause_rate(["rclone", "sync"], {"RCLONE_TRANSFERS": "8"}) == "128K"
    assert pause_rate(["rclone", "sync", "--transfers=2"], {"RCLONE_TRANSFERS": "8"}) == "32K"


@pytest.mark.parametrize(
    ("env", "now", "restored", "detail"),
    [
        ({"RCLONE_BWLIMIT": "8M"}, None, "8M", "The job's own limit"),
        (
            {"RCLONE_BWLIMIT": "08:00,512k 23:00,off"},
            datetime(2026, 11, 16, 9),
            "512k",
            "Now, by the job's timetable",
        ),
        (
            {"RCLONE_BWLIMIT": "08:00,512k 23:00,off"},
            datetime(2026, 11, 16, 23, 30),
            "off",
            "Now, by the job's timetable",
        ),
    ],
)
async def test_resuming_puts_back_the_jobs_own_limit_from_its_environment(
    control_dir, tmp_path, monkeypatch, env, now, restored, detail
):
    if now is not None:
        monkeypatch.setattr(live_control, "_now", lambda: now)
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = env
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    await _known(runner, first.job_run_id)

    await runner.pause(first.job_run_id)
    await runner.resume(first.job_run_id)
    await runner.set_bandwidth(first.job_run_id, "1M")
    await runner.set_bandwidth(first.job_run_id, None)

    # Not "off": resuming and clearing the run's limit go back to what the job set.
    assert [params["rate"] for _, params in rclone.received] == ["64K", restored, "1M", restored]
    assert runner.control_state(first.job_run_id)["detail"] == detail
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_resuming_puts_back_the_limit_rclone_reads_from_the_container(
    control_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv("RCLONE_BWLIMIT", "4M")
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    first = runner.start_job(_job(), trigger="manual")
    await rclone.started.wait()
    await _known(runner, first.job_run_id)

    await runner.pause(first.job_run_id)
    await runner.resume(first.job_run_id)

    assert [params["rate"] for _, params in rclone.received] == ["64K", "4M"]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


def test_job_bandwidth_takes_rclones_separators_and_units():
    # rclone splits timetables on spaces and semicolons.
    limit = job_bandwidth(["rclone", "sync"], {"RCLONE_BWLIMIT": "00:00,1M;23:59,2M"})
    assert limit.is_timetable
    assert limit.rate_at(datetime(2026, 11, 16, 12)) == "1M"
    assert limit.rate_at(datetime(2026, 11, 16, 23, 59)) == "2M"
    # rclone's units, kept as written: rclone reads them again when they are put back.
    for value in ("8MiB", "8Mi", "8m", "512B", "750", "1.5G", "8MiB:1Mi", "08:00,8MiB 23:00,off"):
        assert job_bandwidth(["rclone", "sync", f"--bwlimit={value}"], {}).value == value
    # rclone rejects these, so the step would not start with them.
    for value in ("8MB", "8Q", "fast", "8:", "08:00,8MB"):
        assert job_bandwidth(["rclone", "sync", f"--bwlimit={value}"], {}) is None


def test_rate_labels_and_estimates_read_rclones_units():
    from app.core.estimates import bwlimit_bytes_per_second

    assert rate_label("8MiB") == "8 MiB/s"
    assert rate_label("8Mi") == "8 MiB/s"
    assert rate_label("512B") == "512 B/s"
    assert rate_label("750") == "750 KiB/s"
    assert rate_label("8MiB:1Mi") == "8 MiB/s up, 1 MiB/s down"
    assert live_control.rate_bytes_per_second("8MiB") == 8 * 1024**2
    assert live_control.rate_bytes_per_second("512B") == 512
    assert bwlimit_bytes_per_second("8MiB:1M") == 8 * 1024**2
    assert bwlimit_bytes_per_second("off") is None
    assert bwlimit_bytes_per_second("0") is None


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("UTC", "1M"),  # 07:24 UTC: still the 00:00 slot.
        (":Europe/Helsinki", "2M"),  # 09:24 in Helsinki.
        ("", "1M"),  # Empty is UTC for Go.
        ("Not/AZone", "1M"),  # Unknown is UTC for Go.
    ],
)
def test_timetables_follow_rclones_time_zone_not_the_apps(monkeypatch, tz, expected):
    from datetime import UTC

    monkeypatch.setattr(live_control, "_now", lambda: datetime(2026, 11, 16, 7, 24, tzinfo=UTC))
    limit = job_bandwidth(["rclone", "sync"], {"RCLONE_BWLIMIT": "00:00,1M 08:00,2M", "TZ": tz})
    assert limit.rate_now() == expected


async def test_a_pause_carried_into_a_step_keeps_its_environment_timetable(
    control_dir, tmp_path, monkeypatch
):
    # The next step starts paused. A --bwlimit on its command line would replace the
    # timetable rclone reads from RCLONE_BWLIMIT for good, so the pause goes by the socket.
    monkeypatch.setattr(live_control, "_now", lambda: datetime(2026, 11, 16, 9))
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job(commands=("sync /a remote:a", "sync /b remote:b"))
    job.env = {"RCLONE_BWLIMIT": "08:00,512k 23:00,off"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    await runner.pause(first.job_run_id)
    rclone.release.set()
    while len(rclone.calls) < 2:
        await asyncio.sleep(0.01)
    await rclone.started.wait()

    assert not any(arg.startswith("--bwlimit") for arg in rclone.calls[1])
    while len(rclone.received_by_socket.get("step-2.sock", [])) < 1:
        await asyncio.sleep(0.01)
    assert rclone.received_by_socket["step-2.sock"][0][1]["rate"] == "64K"
    await runner.resume(first.job_run_id)
    assert rclone.received_by_socket["step-2.sock"][-1][1]["rate"] == "512k"
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_the_runner_follows_a_timetable_in_rclones_place(control_dir, tmp_path, monkeypatch):
    # rclone would put its timetable's rate back at each change of slot, ending a pause
    # unseen. The runner replaces it with a single rate and sets each slot's rate itself.
    clock = [datetime(2026, 11, 16, 7, 59)]
    monkeypatch.setattr(live_control, "_now", lambda: clock[0])
    monkeypatch.setattr(runner_module, "FOLLOW_TIMETABLE_SECONDS", 0.01)
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = {"RCLONE_BWLIMIT": "08:00,512k 23:00,off"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    while not rclone.option_calls[1:]:
        await asyncio.sleep(0.01)
    assert rclone.option_calls == [
        ("/options/get", {"blocks": "main"}),
        ("/options/set", {"main": {"BwLimit": "off"}}),
    ]
    assert rclone.received == []  # rclone already has the rate in force.

    async def sent_rates(count):
        while len(rclone.received) < count:
            await asyncio.sleep(0.01)
        return [params["rate"] for _, params in rclone.received]

    clock[0] = datetime(2026, 11, 16, 8, 0)  # The next slot.
    assert await sent_rates(1) == ["512k"]
    await runner.pause(first.job_run_id)
    clock[0] = datetime(2026, 11, 16, 23, 0)  # Another slot while paused: still paused.
    await asyncio.sleep(0.1)
    assert await sent_rates(2) == ["512k", "64K"]
    await runner.resume(first.job_run_id)
    assert await sent_rates(3) == ["512k", "64K", "off"]
    assert len(rclone.option_calls) == 2
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_the_limit_put_back_is_rclones_own_reading_of_the_step(control_dir, tmp_path):
    # rclone takes "--bwlimit=2M" after --exclude as the pattern to exclude, and keeps the
    # environment's 8M: putting back 2M would change the step's limit.
    rclone = FakeRclone(bwlimit="8Mi")  # What rclone answers, in its own spelling.
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job("--exclude --bwlimit=2M")
    job.env = {"RCLONE_BWLIMIT": "8M"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    while not runner._controls[first.job_run_id].job_limit_known:
        await asyncio.sleep(0.01)

    await runner.pause(first.job_run_id)
    await runner.resume(first.job_run_id)

    assert [params["rate"] for _, params in rclone.received] == ["64K", "8Mi"]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_settings_wait_until_rclone_has_told_its_own_limit(
    control_dir, tmp_path, monkeypatch
):
    # Until rclone has said what its own limit is, nothing is sent: it keeps that limit.
    # Once it has, the step gets what it should have.
    rclone = FakeRclone()
    real_read = runner_module.read_rclone_options
    answer = asyncio.Event()

    async def slow_read(socket_path, stop):
        await answer.wait()
        return await real_read(socket_path, stop)

    monkeypatch.setattr(runner_module, "read_rclone_options", slow_read)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = {"RCLONE_BWLIMIT": "8M"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    await runner.pause(first.job_run_id)
    assert rclone.received == []

    answer.set()
    await _known(runner, first.job_run_id)
    while not rclone.received:
        await asyncio.sleep(0.01)
    await runner.resume(first.job_run_id)
    assert [params["rate"] for _, params in rclone.received] == ["64K", "8M"]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_real_rclone_tells_its_own_limit_and_the_runner_takes_over_its_timetable(
    control_dir, tmp_path, monkeypatch
):
    # rclone reads "--bwlimit=2M" after --exclude as the pattern to exclude, and the
    # timetable from RCLONE_BWLIMIT; the runner learns that from rclone itself.
    monkeypatch.setattr(live_control, "_now", lambda: datetime(2026, 11, 16, 12))
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"x" * 4 * 1024 * 1024)
    config = tmp_path / "rclone.conf"
    config.touch()
    job = _job(f"--config {config} --exclude --bwlimit=2M", (f"copy {source} {tmp_path / 'd'}",))
    job.env = {"RCLONE_BWLIMIT": "00:00,100K;23:59,200K", "TZ": "UTC"}
    runner = LiveJobRunner(
        tmp_path / "logs", _session_factory(tmp_path / "runs.db"), control_dir=control_dir
    )
    first = runner.start_job(job, trigger="manual")
    control = runner._controls[first.job_run_id]
    try:
        while not (control.job_limit_known and control.socket_path):
            await asyncio.sleep(0.05)
        assert control.job_limit.is_timetable
        assert control.job_limit.rate_now() == "100Ki"
        socket_path = control.socket_path
        for _ in range(100):
            options = await live_control.read_rclone_options(socket_path, asyncio.Event())
            if options["BwLimit"] == "100Ki":
                break  # Taken over: rclone has a single rate, and no timetable of its own.
            await asyncio.sleep(0.05)
        assert options["BwLimit"] == "100Ki"

        await runner.pause(first.job_run_id)
        assert (await rc_call(socket_path, "core/bwlimit", {}))["rate"] == "64Ki"
        await runner.resume(first.job_run_id)
        assert (await rc_call(socket_path, "core/bwlimit", {}))["rate"] == "100Ki"
    finally:
        runner.cancel_job_run(first.job_run_id)
        await runner.wait_for_job_run(first.job_run_id)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+8M", 8 * 1024**2),
        ("1_024K", 1024 * 1024),
        ("0x1p3M", 8 * 1024**2),
        ("1e3", 1000 * 1024),
        ("8MiB:1M", None),  # A pair: each side separately.
    ],
)
def test_rates_read_numbers_as_rclones_go_parser_does(value, expected):
    if expected is None:
        assert job_bandwidth(["rclone", "sync", f"--bwlimit={value}"], {}).value == value
        return
    assert live_control.rate_bytes_per_second(value) == expected
    assert job_bandwidth(["rclone", "sync", f"--bwlimit={value}"], {}).value == value


@pytest.mark.parametrize("value", ["1__024K", "_8M", "8_M", "0x1M", "-8M", "8.8.8M"])
def test_rates_rclone_rejects_are_not_read(value):
    with pytest.raises(RateError):
        live_control.rate_bytes_per_second(value)


async def test_a_slot_change_before_the_takeover_cant_undo_a_pause(
    control_dir, tmp_path, monkeypatch
):
    # rclone starts in one slot; the next begins before the runner takes over. The runner takes
    # over with the rate rclone has then, so rclone's ticker finds nothing to change later.
    clock = [datetime(2026, 11, 16, 7, 59)]
    monkeypatch.setattr(live_control, "_now", lambda: clock[0])
    rclone = FakeRclone()
    real_read = runner_module.read_rclone_options
    answer = asyncio.Event()

    async def slow_read(socket_path, stop):
        await answer.wait()
        return await real_read(socket_path, stop)

    monkeypatch.setattr(runner_module, "read_rclone_options", slow_read)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = {"RCLONE_BWLIMIT": "08:00,512k 23:00,off"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    clock[0] = datetime(2026, 11, 16, 8, 0)
    rclone.tick()  # rclone moves to the 08:00 slot by itself.
    answer.set()
    await _known(runner, first.job_run_id)
    while len(rclone.option_calls) < 2:
        await asyncio.sleep(0.01)

    await runner.pause(first.job_run_id)
    rclone.tick()  # rclone's next minute: nothing to change.

    assert rclone.state["rate"] == "64K"
    assert rclone.option_calls[-1] == ("/options/set", {"main": {"BwLimit": "512k"}})
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_the_runner_puts_a_pause_back_if_rclone_changed_it(
    control_dir, tmp_path, monkeypatch
):
    # Should rclone's ticker still change the limit (a slot changing just as the runner took
    # over), the runner's next check sets the step's rate again.
    monkeypatch.setattr(runner_module, "FOLLOW_TIMETABLE_SECONDS", 0.01)
    rclone = FakeRclone()
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = {"RCLONE_BWLIMIT": "00:00,1M 23:59,2M"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    await _known(runner, first.job_run_id)
    await runner.pause(first.job_run_id)

    rclone.state["rate"] = "3M"  # Changed behind the runner's back.
    while rclone.state["rate"] != "64K":
        await asyncio.sleep(0.01)
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)


async def test_a_slow_rclone_still_gets_its_own_limit_back(control_dir, tmp_path, monkeypatch):
    # The runner asks for rclone's own limit until rclone answers, however long it takes to
    # start, so that resuming and clearing the run's limit put it back.
    monkeypatch.setattr(live_control, "CONNECT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(live_control, "MAX_CONNECT_RETRY_SECONDS", 0.02)
    rclone = FakeRclone(start_delay=0.5)
    runner = LiveJobRunner(
        tmp_path / "logs",
        _session_factory(tmp_path / "runs.db"),
        executor=rclone,
        control_dir=control_dir,
    )
    job = _job()
    job.env = {"RCLONE_BWLIMIT": "8M"}
    first = runner.start_job(job, trigger="manual")
    await rclone.started.wait()
    await _known(runner, first.job_run_id)

    await runner.pause(first.job_run_id)
    await runner.resume(first.job_run_id)
    await runner.set_bandwidth(first.job_run_id, "1M")
    await runner.set_bandwidth(first.job_run_id, None)

    assert [params["rate"] for _, params in rclone.received] == ["64K", "8M", "1M", "8M"]
    rclone.release.set()
    await runner.wait_for_job_run(first.job_run_id)

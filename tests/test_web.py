import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import app.main as main
from app.core.runner import LiveJobRunner
from app.db import (
    Base,
    ConsoleRunRecord,
    JobRecord,
    JobRunRecord,
    JobStepRecord,
    JobStepRunRecord,
)
from app.main import login_form


def _request(path: str, method: str = "GET") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [], "session": {}})


async def test_health_endpoint_is_public_and_reports_ok():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_login_page_renders_with_current_starlette_template_api():
    request = _request("/login")

    response = await login_form(request)

    assert response.status_code == 200
    assert response.template.name == "login.html"


async def test_login_page_includes_csrf_field():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/login")

    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text


async def test_login_rejects_missing_and_invalid_csrf():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post("/login", data={"username": "admin", "password": "admin"})
        await client.get("/login")
        invalid = await client.post(
            "/login",
            data={"username": "admin", "password": "admin", "csrf_token": "wrong"},
        )

    assert missing.status_code == 403
    assert invalid.status_code == 403


async def test_new_job_page_uses_empty_values_with_hints():
    request = _request("/jobs/new")

    response = await main.new_job(request, None)
    html = response.body.decode()

    assert 'name="name" value=""' in html
    assert 'name="cron" value=""' in html
    assert "Google Drive backups" not in html
    assert "--fast-list --transfers=20 --checkers=40 --verbose --bwlimit ${BW_LIMIT}" in html
    assert 'placeholder="BW_LIMIT=8M"></textarea>' in html
    assert 'placeholder="Sync Music|sync /media/Musiikki remote:/Musiikki"></textarea>' in html
    assert 'placeholder="Nightly media backup"' in html


async def test_jobs_page_receives_ongoing_activity():
    request = _request("/jobs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="running",
                started_at=main.utc_now(),
                ended_at=None,
            )
            db.add(job_run)
            db.flush()
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=job.steps[0].id,
                step_name=job.steps[0].name,
                argv_json='["rclone", "lsd", "secret:"]',
                status="running",
                exit_code=None,
                log_path=str(Path(tmpdir) / "run.log"),
                started_at=job_run.started_at,
                ended_at=None,
            )
            console_run = ConsoleRunRecord(
                status="running",
                command="lsd remote:",
                argv_json='["rclone", "lsd", "remote:"]',
                exit_code=None,
                log_path=str(Path(tmpdir) / "console.log"),
                started_at=job_run.started_at,
                ended_at=None,
            )
            db.add_all([step_run, console_run])
            db.commit()

            response = await main.jobs(request, None, db)

            assert response.context["ongoing_job_runs"][0]["run"].job_name == "backup"
            assert response.context["ongoing_step_runs"][0].step_name == "one"
            assert response.context["ongoing_console_runs"][0].command == "lsd remote:"


@pytest.mark.asyncio
async def test_manual_full_job_redirects_to_live_job_run_before_executor_finishes(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        started.set()
        await release.wait()
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        monkeypatch.setattr(main, "runner", runner)
        with session_factory() as db:
            job = _create_job(db)

            response = await main._run_job(job.id, "manual", False, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/job-runs/1"
            step_run = db.get(JobStepRunRecord, 1)
            assert step_run.status == "running"
            assert step_run.exit_code is None

        await started.wait()
        release.set()
        await runner.wait_for_job_run(1)


@pytest.mark.asyncio
async def test_manual_single_step_run_redirects_to_live_step_log(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        started.set()
        await release.wait()
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        monkeypatch.setattr(main, "runner", runner)
        with session_factory() as db:
            job = _create_job(db)
            step_id = job.steps[0].id

            response = await main._run_job(job.id, "manual-step", False, db, step_id=step_id)

            assert response.status_code == 303
            assert response.headers["location"] == "/runs/1"

        await started.wait()
        release.set()
        await runner.wait_for_job_run(1)


@pytest.mark.asyncio
async def test_run_status_reports_running_and_finished_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            step_run = _create_running_step_run(db)

            running = await main.run_status(step_run.id, None, db)

            assert running["status"] == "running"
            assert running["exit_code"] is None
            assert running["exit_label"] == "Pending"
            assert running["finished_at"] is None
            assert running["can_cancel"] is True
            assert running["elapsed_seconds"] >= 0

            step_run.status = "success"
            step_run.exit_code = 0
            step_run.ended_at = step_run.started_at
            db.commit()

            finished = await main.run_status(step_run.id, None, db)

            assert finished["status"] == "success"
            assert finished["exit_code"] == 0
            assert finished["exit_label"] == "0"
            assert finished["finished_at"] is not None
            assert finished["can_cancel"] is False

            step_run.status = "canceled"
            step_run.exit_code = None
            db.commit()

            canceled = await main.run_status(step_run.id, None, db)

            assert canceled["status"] == "canceled"
            assert canceled["exit_code"] is None
            assert canceled["exit_label"] == "Canceled"
            assert canceled["can_cancel"] is False


async def test_run_log_append_returns_new_bytes():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("first\n", encoding="utf-8")
        offset = log_path.stat().st_size
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("second\n")

            append = await main.run_log_append(step_run.id, None, db, offset=offset)

            assert append == {"text": "second\n", "offset": log_path.stat().st_size}


async def test_job_run_status_includes_running_and_pending_steps():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["first", "second"])
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="running",
                started_at=main.utc_now(),
                ended_at=None,
            )
            db.add(job_run)
            db.flush()
            first_step = job.steps[0]
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=first_step.id,
                step_name=first_step.name,
                argv_json='["rclone", "lsd", "secret:"]',
                status="running",
                exit_code=None,
                log_path=str(Path(tmpdir) / "run.log"),
                started_at=job_run.started_at,
                ended_at=None,
            )
            db.add(step_run)
            db.commit()

            payload = await main.job_run_status(job_run.id, None, db)

            assert payload["status"] == "running"
            assert payload["active_step_run_id"] == step_run.id
            assert payload["can_cancel"] is True
            assert [
                (step["name"], step["status"], step["run_id"]) for step in payload["steps"]
            ] == [
                ("first", "running", step_run.id),
                ("second", "pending", None),
            ]


async def test_job_run_status_marks_unstarted_steps_canceled_after_cancellation():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["first", "second"])
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="canceled",
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add(job_run)
            db.flush()
            first_step = job.steps[0]
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=first_step.id,
                step_name=first_step.name,
                argv_json='["rclone", "lsd", "secret:"]',
                status="canceled",
                exit_code=None,
                log_path=str(Path(tmpdir) / "run.log"),
                started_at=job_run.started_at,
                ended_at=job_run.ended_at,
            )
            db.add(step_run)
            db.commit()

            payload = await main.job_run_status(job_run.id, None, db)

            assert [
                (step["name"], step["status"], step["run_id"]) for step in payload["steps"]
            ] == [
                ("first", "canceled", step_run.id),
                ("second", "canceled", None),
            ]


async def test_job_run_detail_renders_whole_run_tracker():
    request = _request("/job-runs/1")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["first", "second"])
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="running",
                started_at=main.utc_now(),
                ended_at=None,
            )
            db.add(job_run)
            db.flush()
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=job.steps[0].id,
                    step_name="first",
                    argv_json='["rclone", "lsd", "secret:"]',
                    status="running",
                    exit_code=None,
                    log_path=str(Path(tmpdir) / "run.log"),
                    started_at=job_run.started_at,
                    ended_at=None,
                )
            )
            db.commit()

            response = await main.job_run_detail(request, job_run.id, None, db)
            html = response.body.decode()

            assert response.template.name == "job_run_detail.html"
            assert "Current step" in html
            assert "first" in html
            assert "second" in html
            assert "Active log" in html


async def test_history_page_receives_job_runs():
    request = _request("/runs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            db.add(
                JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger="manual",
                    status="success",
                    started_at=main.utc_now(),
                    ended_at=main.utc_now(),
                )
            )
            db.commit()

            response = await main.runs(request, None, db)

            assert response.context["job_runs"][0].job_name == "backup"


async def test_history_page_paginates_independent_tables():
    request = _request("/runs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            for index in range(30):
                job_run = JobRunRecord(
                    job_id=job.id,
                    job_name=f"job-{index}",
                    trigger="manual",
                    status="success",
                    started_at=main.utc_now(),
                    ended_at=main.utc_now(),
                )
                db.add(job_run)
                db.flush()
                db.add(
                    JobStepRunRecord(
                        job_run_id=job_run.id,
                        step_id=job.steps[0].id,
                        step_name=f"step-{index}",
                        argv_json='["rclone", "lsd", "secret:"]',
                        status="success",
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"run-{index}.log"),
                        started_at=job_run.started_at,
                        ended_at=job_run.ended_at,
                    )
                )
                db.add(
                    ConsoleRunRecord(
                        status="success",
                        command=f"version-{index}",
                        argv_json='["rclone", "version"]',
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"console-{index}.log"),
                        started_at=job_run.started_at,
                        ended_at=job_run.ended_at,
                    )
                )
            db.commit()

            response = await main.runs(request, None, db, job_page=2, step_page=2, console_page=2)

            assert len(response.context["job_runs"]) == 5
            assert len(response.context["step_runs"]) == 5
            assert len(response.context["console_runs"]) == 5
            assert response.context["job_pagination"]["page"] == 2
            assert response.context["job_pagination"]["total_pages"] == 2
            assert response.context["job_pagination"]["has_previous"] is True
            assert response.context["job_pagination"]["has_next"] is False
            assert response.context["step_pagination"]["page"] == 2
            assert response.context["step_pagination"]["total_pages"] == 2
            assert response.context["console_pagination"]["page"] == 2
            assert response.context["console_pagination"]["total_pages"] == 2


async def test_settings_prune_reports_deleted_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "old.log"
        log_path.write_text("old", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            step_run.started_at = step_run.started_at.replace(year=2020)
            db.commit()

            response = await main.prune(None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/settings?pruned=1"


async def test_clear_history_removes_run_records_and_logs_but_keeps_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        console_log_path = Path(tmpdir) / "console.log"
        log_path.write_text("run", encoding="utf-8")
        console_log_path.write_text("console", encoding="utf-8")
        with session_factory() as db:
            job = _create_job(db)
            step_run = _create_running_step_run(db, log_path=log_path)
            step_run.job_run.job_id = job.id
            db.add(
                ConsoleRunRecord(
                    status="success",
                    command="version",
                    argv_json='["rclone", "version"]',
                    exit_code=0,
                    log_path=str(console_log_path),
                    started_at=main.utc_now(),
                    ended_at=main.utc_now(),
                )
            )
            db.commit()

            response = await main.clear_history(None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/settings?cleared=2"
            assert db.query(JobRecord).count() == 1
            assert db.query(JobStepRecord).count() == 1
            assert db.query(JobRunRecord).count() == 0
            assert db.query(JobStepRunRecord).count() == 0
            assert db.query(ConsoleRunRecord).count() == 0
            assert not log_path.exists()
            assert not console_log_path.exists()


async def test_cancel_stale_running_job_marks_it_canceled(monkeypatch):
    class FakeRunner:
        def cancel_job_run(self, job_run_id):
            return False

    monkeypatch.setattr(main, "runner", FakeRunner())
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            step_run = _create_running_step_run(db)
            job_run_id = step_run.job_run_id

            response = await main.cancel_job_run(job_run_id, None, None, db)

            assert response.status_code == 303
            job_run = db.get(JobRunRecord, job_run_id)
            assert job_run.status == "canceled"
            assert job_run.ended_at is not None
            assert step_run.status == "canceled"
            assert step_run.ended_at is not None


async def test_delete_individual_job_run_removes_steps_and_logs():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("run", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            job_run_id = step_run.job_run_id

            response = await main.delete_job_run(job_run_id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/runs"
            assert db.get(JobRunRecord, job_run_id) is None
            assert db.get(JobStepRunRecord, step_run.id) is None
            assert not log_path.exists()


async def test_delete_job_removes_configuration_but_keeps_history(monkeypatch):
    synced = []

    def fake_sync_schedules(db):
        synced.append(db)

    monkeypatch.setattr(main, "sync_schedules", fake_sync_schedules)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        console_log_path = Path(tmpdir) / "console.log"
        log_path.write_text("run", encoding="utf-8")
        console_log_path.write_text("console", encoding="utf-8")
        with session_factory() as db:
            job = _create_job(db, step_names=["one", "two"])
            job_id = job.id
            step_ids = [step.id for step in job.steps]
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="success",
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add(job_run)
            db.flush()
            step_run = JobStepRunRecord(
                job_run_id=job_run.id,
                step_id=job.steps[0].id,
                step_name=job.steps[0].name,
                argv_json='["rclone", "lsd", "secret:"]',
                status="success",
                exit_code=0,
                log_path=str(log_path),
                started_at=job_run.started_at,
                ended_at=job_run.ended_at,
            )
            console_run = ConsoleRunRecord(
                status="success",
                command="version",
                argv_json='["rclone", "version"]',
                exit_code=0,
                log_path=str(console_log_path),
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add_all([step_run, console_run])
            db.commit()
            job_run_id = job_run.id
            step_run_id = step_run.id
            console_run_id = console_run.id

            response = await main.delete_job(job_id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/jobs?deleted=1"
            assert db.get(JobRecord, job_id) is None
            for step_id in step_ids:
                assert db.get(JobStepRecord, step_id) is None
            assert db.get(JobRunRecord, job_run_id) is not None
            assert db.get(JobRunRecord, job_run_id).job_id is None
            assert db.get(JobStepRunRecord, step_run_id) is not None
            assert db.get(JobStepRunRecord, step_run_id).step_id is None
            assert db.get(ConsoleRunRecord, console_run_id) is not None
            assert log_path.exists()
            assert console_log_path.exists()
            assert synced == [db]


async def test_delete_job_is_blocked_while_running(monkeypatch):
    synced = []

    def fake_sync_schedules(db):
        synced.append(db)

    monkeypatch.setattr(main, "sync_schedules", fake_sync_schedules)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            job_id = job.id
            step_id = job.steps[0].id
            _create_running_step_run(db)

            response = await main.delete_job(job_id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == f"/jobs/{job_id}?delete_blocked=1"
            assert db.get(JobRecord, job_id) is not None
            assert db.get(JobStepRecord, step_id) is not None
            assert synced == []


async def test_delete_individual_step_run_removes_log_only_for_that_step():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("run", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            job_run_id = step_run.job_run_id

            response = await main.delete_run(step_run.id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/runs"
            assert db.get(JobRunRecord, job_run_id) is not None
            assert db.get(JobStepRunRecord, step_run.id) is None
            assert not log_path.exists()


async def test_delete_individual_console_run_removes_log():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "console.log"
        log_path.write_text("console", encoding="utf-8")
        with session_factory() as db:
            run = ConsoleRunRecord(
                status="success",
                command="version",
                argv_json='["rclone", "version"]',
                exit_code=0,
                log_path=str(log_path),
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add(run)
            db.commit()

            response = await main.delete_console_run(run.id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/runs"
            assert db.get(ConsoleRunRecord, run.id) is None
            assert not log_path.exists()


@pytest.mark.asyncio
async def test_cancel_run_requests_parent_job_cancellation(monkeypatch):
    called = []

    class FakeRunner:
        def cancel_job_run(self, job_run_id):
            called.append(job_run_id)
            return True

    monkeypatch.setattr(main, "runner", FakeRunner())
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            step_run = _create_running_step_run(db)

            response = await main.cancel_run(step_run.id, None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == f"/runs/{step_run.id}"
            assert called == [step_run.job_run_id]


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _create_job(db, step_names: list[str] | None = None):
    job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
    for position, name in enumerate(step_names or ["one"], start=1):
        job.steps.append(JobStepRecord(position=position, name=name, command="lsd secret:"))
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create_running_step_run(db, log_path: Path | None = None):
    job_run = JobRunRecord(
        job_id=1,
        job_name="backup",
        trigger="manual",
        status="running",
        started_at=main.utc_now(),
        ended_at=None,
    )
    db.add(job_run)
    db.flush()
    step_run = JobStepRunRecord(
        job_run_id=job_run.id,
        step_id=1,
        step_name="one",
        argv_json='["rclone", "lsd", "secret:"]',
        status="running",
        exit_code=None,
        log_path=str(log_path or Path("missing.log")),
        started_at=job_run.started_at,
        ended_at=None,
    )
    db.add(step_run)
    db.commit()
    db.refresh(step_run)
    return step_run

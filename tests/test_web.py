import asyncio
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import app.main as main
from app.core.runner import LiveJobRunner
from app.db import Base, JobRecord, JobRunRecord, JobStepRecord, JobStepRunRecord
from app.main import login_form


async def test_login_page_renders_with_current_starlette_template_api():
    request = Request({"type": "http", "method": "GET", "path": "/login", "headers": []})

    response = await login_form(request)

    assert response.status_code == 200
    assert response.template.name == "login.html"


async def test_new_job_page_uses_empty_values_with_hints():
    request = Request({"type": "http", "method": "GET", "path": "/jobs/new", "headers": []})

    response = await main.new_job(request, None)
    html = response.body.decode()

    assert 'name="name" value=""' in html
    assert 'name="cron" value=""' in html
    assert "Google Drive backups" not in html
    assert "--fast-list --transfers=20 --checkers=40 --verbose --bwlimit ${BW_LIMIT}" in html
    assert 'placeholder="BW_LIMIT=8M"></textarea>' in html
    assert 'placeholder="Sync Music|sync /media/Musiikki remote:/Musiikki"></textarea>' in html
    assert 'placeholder="Nightly media backup"' in html


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
    request = Request({"type": "http", "method": "GET", "path": "/job-runs/1", "headers": []})
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
    request = Request({"type": "http", "method": "GET", "path": "/runs", "headers": []})
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

            response = await main.cancel_run(step_run.id, None, db)

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

import asyncio
import base64
import json
import shutil
import tempfile
import warnings
from pathlib import Path

import httpx
import itsdangerous
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main
from app.config import settings
from app.core.notifications import load_email_notification_settings
from app.core.rclone_stats import STATS_UNAVAILABLE_JSON, RcloneTransferStats, stats_to_json
from app.core.runner import LiveJobRunner
from app.db import (
    Base,
    ConsoleRunRecord,
    JobRecord,
    JobRunRecord,
    JobStepRecord,
    JobStepRunRecord,
    SourceMeasurementRecord,
    record_to_job,
)
from app.main import login_form


def _request(path: str, method: str = "GET") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [], "session": {}})


def test_create_app_does_not_emit_lifecycle_deprecation_warnings():
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)

        main.create_app()

    assert not [
        warning
        for warning in captured
        if issubclass(warning.category, DeprecationWarning) and "on_event" in str(warning.message)
    ]


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


async def test_login_is_throttled_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(main, "login_throttle", main.LoginThrottle(max_failures=3))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/login")
        token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        failures = [
            await client.post(
                "/login", data={"username": "admin", "password": "wrong", "csrf_token": token}
            )
            for _ in range(3)
        ]
        blocked = await client.post(
            "/login",
            data={"username": settings.admin_user, "password": "admin", "csrf_token": token},
        )

    assert [response.status_code for response in failures] == [401, 401, 401]
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert "Too many failed logins" in blocked.text


async def test_new_job_page_uses_empty_values_with_hints():
    request = _request("/jobs/new")

    response = await main.new_job(request, None)
    html = response.body.decode()

    assert 'name="name" value=""' in html
    assert 'name="cron" value=""' in html
    assert "Google Drive backups" not in html
    assert "--transfers=4 --checkers=8 --verbose --bwlimit ${BW_LIMIT}" in html
    assert "--fast-list" not in html
    assert 'placeholder="BW_LIMIT=8M"></textarea>' in html
    assert 'placeholder="Sync Music|sync /media/Musiikki remote:/Musiikki"></textarea>' in html
    assert 'placeholder="Nightly media backup"' in html


async def test_jobs_page_receives_running_now_rows():
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

            job_row, console_row = response.context["running_now"]
            assert job_row["kind"] == "job"
            assert job_row["name"] == "backup"
            assert job_row["detail"] == "Step: one"
            assert job_row["url"] == f"/job-runs/{job_run.id}"
            assert job_row["progress"] is None
            assert job_row["elapsed_seconds"] >= 0
            assert console_row["kind"] == "console"
            assert console_row["name"] == "lsd remote:"
            assert console_row["url"] == f"/console/runs/{console_run.id}"


async def test_jobs_page_includes_last_and_next_run_context():
    request = _request("/jobs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            job.cron = "0 2 * * *"
            older_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual",
                status="failed",
                started_at=main.utc_now().replace(year=2025),
                ended_at=main.utc_now().replace(year=2025),
            )
            newest_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="schedule",
                status="success",
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add_all([older_run, newest_run])
            db.commit()

            response = await main.jobs(request, None, db)
            row = response.context["jobs"][0]

            assert row["last_run"] == newest_run
            assert row["next_run"] is not None


async def test_jobs_page_fetches_latest_runs_in_batch():
    request = _request("/jobs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            first_job = _create_job(db)
            second_job = _create_job(db)
            second_job.name = "archive"
            db.add_all(
                [
                    JobRunRecord(
                        job_id=first_job.id,
                        job_name=first_job.name,
                        trigger="manual",
                        status="success",
                        started_at=main.utc_now(),
                        ended_at=main.utc_now(),
                    ),
                    JobRunRecord(
                        job_id=second_job.id,
                        job_name=second_job.name,
                        trigger="manual",
                        status="failed",
                        started_at=main.utc_now(),
                        ended_at=main.utc_now(),
                    ),
                ]
            )
            db.commit()
            job_run_selects = []

            def track_job_run_selects(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT") and "job_runs" in statement:
                    job_run_selects.append(statement)

            event.listen(db.bind, "before_cursor_execute", track_job_run_selects)
            try:
                await main.jobs(request, None, db)
            finally:
                event.remove(db.bind, "before_cursor_execute", track_job_run_selects)

            assert len(job_run_selects) == 2


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

            assert append == {
                "text": "second\n",
                "offset": log_path.stat().st_size,
                "start": offset,
                "more": False,
            }


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


async def test_job_run_status_includes_stats_labels_for_live_refresh():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
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
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=job.steps[0].id,
                    step_name=job.steps[0].name,
                    argv_json='["rclone", "sync"]',
                    status="success",
                    exit_code=0,
                    log_path=str(Path(tmpdir) / "missing.log"),
                    started_at=job_run.started_at,
                    ended_at=job_run.ended_at,
                    transfer_stats_json=(
                        '{"transferred_bytes": 1048576, "transferred_files": 3, "deleted_files": 1}'
                    ),
                )
            )
            db.commit()

            payload = await main.job_run_status(job_run.id, None, db)

            assert payload["stats"]["transferred_data"] == "1.000 MiB"
            assert payload["stats"]["transferred_files"] == "3"
            assert payload["stats"]["deleted_files"] == "1"
            assert payload["steps"][0]["stats_label"] == "1.000 MiB, 3 files, 1 deleted"
            assert payload["steps"][0]["started_at"] is not None
            assert payload["steps"][0]["duration"] == "0.0s"


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
            assert 'data-step-exit="true"' in html
            assert 'data-step-log="true"' in html
            assert "exitCell.textContent = step.exit_label ??" in html
            assert "logCell.innerHTML" in html


async def test_job_run_detail_renders_run_totals_and_step_stats_from_stored_stats():
    request = _request("/job-runs/1")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
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
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=job.steps[0].id,
                    step_name=job.steps[0].name,
                    argv_json='["rclone", "sync"]',
                    status="success",
                    exit_code=0,
                    log_path=str(Path(tmpdir) / "missing.log"),
                    started_at=job_run.started_at,
                    ended_at=job_run.ended_at,
                    transfer_stats_json=(
                        '{"transferred_bytes": 2097152, "transferred_files": 5, "deleted_files": 2}'
                    ),
                )
            )
            db.commit()

            response = await main.job_run_detail(request, job_run.id, None, db)
            html = response.body.decode()

            assert "Data transferred" in html
            assert "2.000 MiB" in html
            assert "Files transferred" in html
            assert "Deleted files" in html
            assert "<th>Stats</th>" in html
            assert "<th>Timing</th>" in html
            assert "Started" in html
            assert "Duration" in html
            assert "2.000 MiB, 5 files, 2 deleted" in html
            assert 'id="job-run-data-transferred"' in html


async def test_job_run_detail_renders_not_available_for_completed_step_without_stats():
    request = _request("/job-runs/1")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
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
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=job.steps[0].id,
                    step_name=job.steps[0].name,
                    argv_json='["rclone", "sync"]',
                    status="success",
                    exit_code=0,
                    log_path=str(Path(tmpdir) / "missing.log"),
                    started_at=job_run.started_at,
                    ended_at=job_run.ended_at,
                )
            )
            db.commit()

            response = await main.job_run_detail(request, job_run.id, None, db)
            html = response.body.decode()

            # rclone prints its totals only at some log levels: no totals, no claim.
            assert "No changes" not in html
            assert "Not available" in html
            history = main._history_row("jobs", job_run)
            assert history["transfer"] == {"data": "Not available", "detail": None}


async def test_run_detail_renders_step_stats_from_log_fallback_for_existing_runs():
    request = _request("/runs/1")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text(
            "\n".join(
                [
                    "Transferred:      512 KiB / 512 KiB, 100%",
                    "Transferred:          4 / 4, 100%",
                ]
            ),
            encoding="utf-8",
        )
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            step_run.status = "success"
            step_run.exit_code = 0
            step_run.ended_at = step_run.started_at
            db.commit()

            response = await main.run_detail(request, step_run.id, None, db)
            html = response.body.decode()

            assert "Data transferred" in html
            assert "512 KiB" in html
            assert "Files transferred" in html
            assert "4" in html
            assert "Deleted files" in html


async def test_history_page_summarizes_finished_job_runs():
    request = _request("/runs")
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("", encoding="utf-8")
        with session_factory() as db:
            step_run = _finish(db, _create_running_step_run(db, log_path=log_path))
            step_run.transfer_stats_json = stats_to_json(
                RcloneTransferStats(
                    transferred_bytes=3 * 1024**3, transferred_files=12, deleted_files=2
                )
            )
            db.commit()

            response = await main.runs(request, None, db)
            html = response.body.decode()

            (row,) = response.context["rows"]
            assert row["run"].job_name == "backup"
            assert row["transfer"] == {"data": "3.000 GiB", "detail": "12 files, 2 deleted"}
            assert "3.000 GiB" in html
            assert "12 files, 2 deleted" in html
            assert response.context["view"] == "jobs"
            assert [tab["count"] for tab in response.context["tabs"]] == [1, 1, 0]


async def test_history_transfer_summary_handles_empty_and_failed_runs():
    assert main._transfer_summary(0, 0, 0, "success") == {
        "data": "Nothing transferred",
        "detail": None,
    }
    assert main._transfer_summary(0, 0, 0, "failed") == {"data": "-", "detail": None}
    assert main._transfer_summary(512, 1, 0, "failed") == {"data": "512 B", "detail": "1 file"}


async def test_history_page_shows_running_runs_without_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            _create_running_step_run(db, log_path=Path(tmpdir) / "run.log")

            jobs_view = await main.runs(_request("/runs"), None, db)
            steps_view = await main.runs(_request("/runs"), None, db, view="steps")

            for response in (jobs_view, steps_view):
                (row,) = response.context["rows"]
                assert row["duration"] == "-"
                assert row["transfer"] is None
                assert '<span class="run-status running">Running</span>' in response.body.decode()


async def test_history_page_shows_one_view_at_a_time_with_pagination():
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
            for index in range(3):
                db.add(
                    ConsoleRunRecord(
                        status="success",
                        command=f"version-{index}",
                        argv_json='["rclone", "version"]',
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"console-{index}.log"),
                        started_at=main.utc_now(),
                        ended_at=main.utc_now(),
                    )
                )
            db.commit()

            steps = await main.runs(request, None, db, view="steps", page=2)
            console = await main.runs(request, None, db, view="console", page=9)
            unknown = await main.runs(request, None, db, view="nope")

            assert steps.context["view"] == "steps"
            assert len(steps.context["rows"]) == 5
            assert steps.context["pagination"] == {
                "page": 2,
                "total_pages": 2,
                "total_items": 30,
                "first_item": 26,
                "last_item": 30,
                "has_previous": True,
                "has_next": False,
                "previous_url": "/runs?view=steps",
                "next_url": None,
            }
            assert [tab["count"] for tab in steps.context["tabs"]] == [30, 30, 3]
            assert [tab["current"] for tab in steps.context["tabs"]] == [False, True, False]
            # A page past the end shows the last page instead of an empty table.
            assert console.context["pagination"]["page"] == 1
            assert len(console.context["rows"]) == 3
            assert unknown.context["view"] == "jobs"
            assert len(unknown.context["rows"]) == 25
            assert unknown.context["pagination"]["next_url"] == "/runs?page=2"


async def test_settings_prune_reports_deleted_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "old.log"
        log_path.write_text("old", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            step_run.started_at = step_run.started_at.replace(year=2020)
            for record in (step_run, step_run.job_run):
                record.status = "success"
                record.ended_at = step_run.started_at
            db.commit()

            response = await main.prune(None, None, db)

            assert response.status_code == 303
            assert response.headers["location"] == "/settings?pruned=1"


async def test_save_email_settings_persists_values_and_preserves_blank_password():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            response = await main.save_email_settings(
                None,
                db,
                None,
                enabled="on",
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username="user@gmail.com",
                smtp_password="app-password",
                sender="user@gmail.com",
                recipients="ops@example.com\nadmin@example.com",
                use_starttls="on",
                notify_success=None,
                notify_failure="on",
                notify_canceled="on",
                app_base_url="http://runner.local",
                include_log_tail_lines=200,
            )
            second = await main.save_email_settings(
                None,
                db,
                None,
                enabled=None,
                smtp_host="smtp.example.com",
                smtp_port=2525,
                smtp_username="other@example.com",
                smtp_password="",
                sender="sender@example.com",
                recipients="ops@example.com",
                use_starttls=None,
                use_ssl="on",
                notify_success="on",
                notify_failure="on",
                notify_canceled=None,
                app_base_url="",
                include_log_tail_lines=50,
            )

            saved = load_email_notification_settings(db)
            assert saved.use_ssl is True
            assert saved.use_starttls is False

            assert response.headers["location"] == "/settings?email_saved=1"
            assert second.headers["location"] == "/settings?email_saved=1"
            assert saved.enabled is False
            assert saved.smtp_host == "smtp.example.com"
            assert saved.smtp_password == "app-password"
            assert saved.notify_success is True
            assert saved.notify_canceled is False


async def test_send_test_email_reports_success_and_failure(monkeypatch):
    calls = []

    async def fake_send_test_notification(settings):
        calls.append(settings.smtp_host)

    monkeypatch.setattr(main, "send_test_notification", fake_send_test_notification)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            await main.save_email_settings(
                None,
                db,
                None,
                enabled="on",
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username="user@gmail.com",
                smtp_password="app-password",
                sender="user@gmail.com",
                recipients="ops@example.com",
                use_starttls="on",
                notify_success=None,
                notify_failure="on",
                notify_canceled="on",
                app_base_url="",
                include_log_tail_lines=200,
            )

            response = await main.send_test_email(
                None,
                db,
                None,
                enabled="on",
                smtp_host="mail.example.com",
                smtp_port=2525,
                smtp_username="notify@example.com",
                smtp_password="",
                sender="notify@example.com",
                recipients="ops@example.com",
                use_starttls=None,
                notify_success="on",
                notify_failure="on",
                notify_canceled=None,
                app_base_url="http://runner.local",
                include_log_tail_lines=50,
            )
            saved = load_email_notification_settings(db)

            assert response.headers["location"] == "/settings?email_test=sent"
            assert calls == ["mail.example.com"]
            assert saved.smtp_host == "mail.example.com"
            assert saved.smtp_password == "app-password"

    async def failing_send_test_notification(settings):
        raise ValueError("bad smtp")

    monkeypatch.setattr(main, "send_test_notification", failing_send_test_notification)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            response = await main.send_test_email(
                None,
                db,
                None,
                enabled="on",
                smtp_host="mail.example.com",
                smtp_port=2525,
                smtp_username="notify@example.com",
                smtp_password="app-password",
                sender="notify@example.com",
                recipients="ops@example.com",
                use_starttls="on",
                notify_success=None,
                notify_failure="on",
                notify_canceled="on",
                app_base_url="",
                include_log_tail_lines=200,
            )

            assert response.headers["location"] == "/settings?email_error=bad+smtp"


async def test_clear_history_removes_run_records_and_logs_but_keeps_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        console_log_path = Path(tmpdir) / "console.log"
        log_path.write_text("run", encoding="utf-8")
        console_log_path.write_text("console", encoding="utf-8")
        with session_factory() as db:
            job = _create_job(db)
            step_run = _finish(db, _create_running_step_run(db, log_path=log_path))
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
            step_run = _finish(db, _create_running_step_run(db, log_path=log_path))
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
            step_run = _finish(db, _create_running_step_run(db, log_path=log_path))
            job_run_id = step_run.job_run_id

            response = await main.delete_run(step_run.id, None, None, db, page=3)

            assert response.status_code == 303
            assert response.headers["location"] == "/runs?view=steps&page=3"
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
            assert response.headers["location"] == "/runs?view=console"
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


def _job_form(**overrides):
    values = {
        "name": "backup",
        "cron": "0 5 * * *",
        "common_args": "--verbose",
        "env_lines": "",
        "steps_text": "one|lsd secret:",
        "enabled": "on",
    }
    return values | overrides


async def test_create_job_rejects_invalid_cron_and_keeps_submitted_values(monkeypatch):
    synced = []
    monkeypatch.setattr(main, "sync_schedules", lambda db: synced.append(db))
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            response = await main.create_job(
                _request("/jobs", "POST"),
                None,
                None,
                db,
                **_job_form(cron="0 25 * * *", common_args="--transfers=4"),
            )
            html = response.body.decode()

            assert response.status_code == 400
            assert "Invalid cron schedule" in html
            assert 'name="cron" value="0 25 * * *"' in html
            assert "--transfers=4</textarea>" in html
            assert db.query(JobRecord).count() == 0
    assert synced == []


async def test_update_job_rejects_invalid_cron_without_saving(monkeypatch):
    monkeypatch.setattr(main, "sync_schedules", lambda db: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            job.cron = "0 5 * * *"
            db.commit()

            response = await main.update_job(
                _request(f"/jobs/{job.id}", "POST"),
                job.id,
                None,
                None,
                db,
                **_job_form(name="renamed", cron="0 0 5 * * *"),
            )
            html = response.body.decode()

            assert response.status_code == 400
            assert "5 fields" in html
            assert 'name="name" value="renamed"' in html
            db.expire_all()
            saved = db.get(JobRecord, job.id)
            assert saved.name == "backup"
            assert saved.cron == "0 5 * * *"


async def test_update_job_keeps_step_ids_so_history_stays_attached(monkeypatch):
    monkeypatch.setattr(main, "sync_schedules", lambda db: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["first", "second", "third"])
            first_id, second_id, third_id = (step.id for step in job.steps)
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="schedule",
                status="success",
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add(job_run)
            db.flush()
            for step in job.steps:
                db.add(
                    JobStepRunRecord(
                        job_run_id=job_run.id,
                        step_id=step.id,
                        step_name=step.name,
                        argv_json="[]",
                        status="success",
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"{step.name}.log"),
                        started_at=job_run.started_at,
                        ended_at=job_run.ended_at,
                    )
                )
            db.commit()

            await main.update_job(
                _request(f"/jobs/{job.id}", "POST"),
                job.id,
                None,
                None,
                db,
                **_job_form(steps_text="second|lsd holvi:\nfirst|lsd holvi:\nnew|lsd holvi:"),
            )

            db.expire_all()
            steps = db.get(JobRecord, job.id).steps
            assert [(step.name, step.position, step.command) for step in steps] == [
                ("second", 1, "lsd holvi:"),
                ("first", 2, "lsd holvi:"),
                ("new", 3, "lsd holvi:"),
            ]
            assert steps[0].id == second_id
            assert steps[1].id == first_id
            assert steps[2].id not in {first_id, second_id, third_id}
            removed_run = db.query(JobStepRunRecord).filter_by(step_name="third").one()
            assert removed_run.step_id is None


async def test_update_job_matches_duplicate_step_names_in_order(monkeypatch):
    monkeypatch.setattr(main, "sync_schedules", lambda db: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["same", "same"])
            first_id, second_id = (step.id for step in job.steps)

            await main.update_job(
                _request(f"/jobs/{job.id}", "POST"),
                job.id,
                None,
                None,
                db,
                **_job_form(steps_text="same|lsd a:\nsame|lsd b:\nsame|lsd c:"),
            )

            db.expire_all()
            steps = db.get(JobRecord, job.id).steps
            assert [step.command for step in steps] == ["lsd a:", "lsd b:", "lsd c:"]
            assert [step.id for step in steps[:2]] == [first_id, second_id]
            assert steps[2].id not in {first_id, second_id}


async def test_job_run_rows_show_recorded_steps_when_steps_no_longer_exist():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["current"])
            job_run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="schedule",
                status="success",
                started_at=main.utc_now(),
                ended_at=main.utc_now(),
            )
            db.add(job_run)
            db.flush()
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=999,
                    step_name="old step",
                    argv_json="[]",
                    status="success",
                    exit_code=0,
                    log_path=str(Path(tmpdir) / "old.log"),
                    started_at=job_run.started_at,
                    ended_at=job_run.ended_at,
                )
            )
            db.commit()

            rows = main._job_run_step_rows(job_run, db)

            assert [(row["name"], row["status"]) for row in rows] == [("old step", "success")]


def _session_cookie(session: dict) -> str:
    data = base64.b64encode(json.dumps(session).encode())
    return itsdangerous.TimestampSigner(str(settings.secret_key)).sign(data).decode()


def test_console_terminal_rejects_cross_origin_handshake():
    client = TestClient(main.app)
    client.cookies.set("session", _session_cookie({main.SESSION_KEY: True}))

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            "/console/terminal", headers={"origin": "http://192.168.1.3:9999"}
        ),
    ):
        pass

    assert exc_info.value.code == 1008


def test_console_terminal_accepts_same_origin_handshake():
    client = TestClient(main.app)
    client.cookies.set("session", _session_cookie({main.SESSION_KEY: True}))

    with client.websocket_connect(
        "/console/terminal", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_json() == {"type": "output", "data": "rclone> "}


def test_console_terminal_accepts_origin_matching_forwarded_host():
    client = TestClient(main.app)
    client.cookies.set("session", _session_cookie({main.SESSION_KEY: True}))

    with client.websocket_connect(
        "/console/terminal",
        headers={"origin": "https://backup.example", "x-forwarded-host": "backup.example"},
    ) as websocket:
        assert websocket.receive_json() == {"type": "output", "data": "rclone> "}


def _finish(db, step_run):
    step_run.status = "success"
    step_run.exit_code = 0
    step_run.ended_at = main.utc_now()
    step_run.job_run.status = "success"
    step_run.job_run.ended_at = step_run.ended_at
    db.commit()
    return step_run


async def test_running_runs_cannot_be_deleted():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("run", encoding="utf-8")
        console_log = Path(tmpdir) / "console.log"
        console_log.write_text("console", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)
            console_run = ConsoleRunRecord(
                status="running",
                command="config",
                argv_json='["rclone", "config"]',
                exit_code=None,
                log_path=str(console_log),
                started_at=main.utc_now(),
                ended_at=None,
            )
            db.add(console_run)
            db.commit()

            responses = [
                await main.delete_job_run(step_run.job_run_id, None, None, db),
                await main.delete_run(step_run.id, None, None, db),
                await main.delete_console_run(console_run.id, None, None, db),
            ]

            assert [response.headers["location"] for response in responses] == [
                "/runs?delete_blocked=1",
                "/runs?view=steps&delete_blocked=1",
                "/runs?view=console&delete_blocked=1",
            ]
            assert db.get(JobRunRecord, step_run.job_run_id) is not None
            assert db.get(JobStepRunRecord, step_run.id) is not None
            assert db.get(ConsoleRunRecord, console_run.id) is not None
            assert log_path.exists()
            assert console_log.exists()

            page = await main.runs(_request("/runs"), None, db, delete_blocked=True)
            assert "still in progress" in page.body.decode()


async def test_clear_history_keeps_runs_in_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        running_log = Path(tmpdir) / "running.log"
        finished_log = Path(tmpdir) / "finished.log"
        running_log.write_text("running", encoding="utf-8")
        finished_log.write_text("finished", encoding="utf-8")
        with session_factory() as db:
            running = _create_running_step_run(db, log_path=running_log)
            running_ids = (running.id, running.job_run_id)
            finished = _finish(db, _create_running_step_run(db, log_path=finished_log))
            finished_ids = (finished.id, finished.job_run_id)

            response = await main.clear_history(None, None, db)

            assert response.headers["location"] == "/settings?cleared=1&kept=1"
            db.expire_all()
            assert db.get(JobStepRunRecord, running_ids[0]) is not None
            assert db.get(JobRunRecord, running_ids[1]) is not None
            assert db.get(JobStepRunRecord, finished_ids[0]) is None
            assert db.get(JobRunRecord, finished_ids[1]) is None
            assert running_log.exists()
            assert not finished_log.exists()


def test_console_marks_command_failed_when_its_output_cannot_be_written(monkeypatch):
    async def failing_bridge(argv, receive_text, send_text):
        raise OSError(28, "No space left on device")

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        monkeypatch.setattr(main, "SessionLocal", session_factory)
        monkeypatch.setattr(main, "bridge_pty", failing_bridge)
        monkeypatch.setattr(
            main, "_console_log_path", lambda started_at: Path(tmpdir) / "console.log"
        )
        client = TestClient(main.app)
        client.cookies.set("session", _session_cookie({main.SESSION_KEY: True}))

        with client.websocket_connect(
            "/console/terminal", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json() == {"type": "output", "data": "rclone> "}
            assert websocket.receive_json() == {"type": "state", "state": "idle"}
            websocket.send_json({"type": "command", "command": "version"})
            messages = []
            while {"type": "state", "state": "idle"} not in messages:
                messages.append(websocket.receive_json())

        with session_factory() as db:
            record = db.query(ConsoleRunRecord).one()
            assert record.status == "failed"
            assert record.ended_at is not None
        assert any("No space left" in message.get("data", "") for message in messages)
        assert "Console command failed" in (Path(tmpdir) / "console.log").read_text()


async def test_running_job_run_reports_live_progress_from_active_step_log():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text(
            "Transferred:   \t  412.000 GiB / 2.000 TiB, 20%, 57.000 MiB/s, ETA 7h50m0s\n",
            encoding="utf-8",
        )
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)

            payload = await main.job_run_status(step_run.job_run_id, None, db)
            step_payload = await main.run_status(step_run.id, None, db)
            page = await main.job_run_detail(_request("/job-runs/1"), step_run.job_run_id, None, db)

            expected = "412.000 GiB / 2.000 TiB (20%) · 57.000 MiB/s · ETA 7h50m0s"
            assert payload["progress"] == expected
            assert step_payload["progress"] == expected
            assert expected in page.body.decode()
            jobs_page = await main.jobs(_request("/jobs"), None, db)
            assert jobs_page.context["running_now"][0]["progress"] == expected
            assert expected in jobs_page.body.decode()

            _finish(db, step_run)
            payload = await main.job_run_status(step_run.job_run_id, None, db)
            page = await main.job_run_detail(_request("/job-runs/1"), step_run.job_run_id, None, db)

            assert payload["progress"] is None
            assert 'id="job-run-progress"' not in page.body.decode()


async def test_running_step_page_explains_missing_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text("starting\n", encoding="utf-8")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)

            page = await main.run_detail(_request("/runs/1"), step_run.id, None, db)

            assert "rclone reports progress every minute with --verbose" in page.body.decode()


async def test_duplicate_job_copies_configuration_disabled():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db, step_names=["Akkeli", "Musa"])
            job.cron = "0 5 * * *"
            job.common_args = "--transfers=4 --verbose"
            job.env_json = '{"BW_LIMIT": "8M"}'
            db.commit()

            response = await main.duplicate_job(job.id, None, None, db)

            copy = db.query(JobRecord).filter(JobRecord.id != job.id).one()
            assert response.headers["location"] == f"/jobs/{copy.id}?duplicated=1"
            assert copy.name == "backup (copy)"
            assert copy.enabled is False
            assert copy.cron == "0 5 * * *"
            assert copy.common_args == "--transfers=4 --verbose"
            assert copy.env_json == '{"BW_LIMIT": "8M"}'
            assert [(step.position, step.name, step.command) for step in copy.steps] == [
                (step.position, step.name, step.command) for step in job.steps
            ]
            assert {step.id for step in copy.steps}.isdisjoint({step.id for step in job.steps})

            page = await main.job_detail(_request("/jobs/2"), copy.id, None, db, duplicated=True)
            assert "This is a copy, and it is disabled." in page.body.decode()


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


def test_exit_label_marks_runs_that_ended_without_an_exit_code():
    assert main._exit_label("running", None) == "Pending"
    assert main._exit_label("failed", None) == "-"
    assert main._exit_label("canceled", None) == "Canceled"
    assert main._exit_label("failed", 1) == "1"


async def test_step_status_allows_delete_only_after_the_whole_job_finishes():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=Path(tmpdir) / "run.log")
            assert (await main.run_status(step_run.id, None, db))["can_delete"] is False

            # The step is done but a later step of the same job is still running.
            step_run.status = "success"
            step_run.exit_code = 0
            step_run.ended_at = main.utc_now()
            db.commit()
            assert (await main.run_status(step_run.id, None, db))["can_delete"] is False

            _finish(db, step_run)
            assert (await main.run_status(step_run.id, None, db))["can_delete"] is True


class FakeControlRunner:
    def __init__(self, error: Exception | None = None, paused: bool = False):
        self.error = error
        self.calls: list[tuple] = []
        self.paused = paused

    def control_state(self, job_run_id):
        return {
            "available": True,
            "paused": self.paused,
            "paused_now": self.paused,
            "limit": "8M",
            "limit_label": "8 MiB/s",
            "label": "Paused" if self.paused else "8 MiB/s",
            "detail": "Throttled to 64 KiB/s" if self.paused else "Set for this run",
            "source": "run",
        }

    async def _record(self, *call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    def set_bandwidth(self, job_run_id, rate):
        return self._record("bandwidth", job_run_id, rate)

    def pause(self, job_run_id):
        return self._record("pause", job_run_id)

    def resume(self, job_run_id):
        return self._record("resume", job_run_id)


async def test_bandwidth_routes_drive_the_runner_and_return_to_the_run(monkeypatch):
    fake = FakeControlRunner()
    monkeypatch.setattr(main, "runner", fake)

    responses = [
        await main.set_job_run_bandwidth(3, None, None, rate="10M", custom=""),
        await main.set_job_run_bandwidth(3, None, None, rate="job", custom=" 750k "),
        await main.set_job_run_bandwidth(3, None, None, rate="job", custom=""),
        await main.pause_job_run(3, None, None),
        await main.resume_job_run(3, None, None),
    ]

    assert {response.headers["location"] for response in responses} == {"/job-runs/3"}
    assert fake.calls == [
        ("bandwidth", 3, "10M"),
        ("bandwidth", 3, "750k"),
        ("bandwidth", 3, None),
        ("pause", 3),
        ("resume", 3),
    ]


async def test_bandwidth_route_errors_are_shown_on_the_run_page(monkeypatch):
    from app.core.live_control import RateError, RcError

    for error, code in (
        (RateError("bad"), "rate"),
        (KeyError(3), "ended"),
        (RcError("down"), "unreachable"),
    ):
        monkeypatch.setattr(main, "runner", FakeControlRunner(error))
        response = await main.pause_job_run(3, None, None)
        assert response.headers["location"] == f"/job-runs/3?control_error={code}"

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=Path(tmpdir) / "run.log")
            monkeypatch.setattr(main, "runner", FakeControlRunner(paused=True))

            page = await main.job_run_detail(
                _request("/job-runs/1"), step_run.job_run_id, None, db, control_error="rate"
            )
            html = page.body.decode()
            status = await main.job_run_status(step_run.job_run_id, None, db)

    assert 'id="run-control"' in html
    assert "That is not a bandwidth limit" in html
    assert f'action="/job-runs/{step_run.job_run_id}/resume"' in html
    assert '<option value="8M" selected>8 MiB/s</option>' in html
    assert status["control"]["paused"] is True
    # The page refreshes the explanation when a step change makes the pause pending or live.
    assert (
        '<span class="table-subtext" id="run-control-detail">Throttled to 64 KiB/s</span>' in html
    )
    assert "state.control.detail" in html
    assert status["progress"] == "Paused"


async def test_paused_progress_drops_speed_and_eta(monkeypatch):
    monkeypatch.setattr(main, "runner", FakeControlRunner(paused=True))
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "run.log"
        log_path.write_text(
            "Transferred:   \t  412.000 GiB / 2.000 TiB, 20%, 57.000 MiB/s, ETA 7h50m0s\n",
            encoding="utf-8",
        )
        with session_factory() as db:
            step_run = _create_running_step_run(db, log_path=log_path)

            payload = await main.job_run_status(step_run.job_run_id, None, db)
            jobs_page = await main.jobs(_request("/jobs"), None, db)

    assert payload["progress"] == "Paused · 412.000 GiB / 2.000 TiB (20%)"
    assert jobs_page.context["running_now"][0]["progress"] == payload["progress"]


class FakeMeasurer:
    def __init__(self):
        self.started = []

    def start(self, commands, env):
        self.started.append(([command.source for command in commands], env))
        return len(commands)


def _step_digest(job, step):
    """The configuration digest the runner records for a step run of this job."""
    argv = next(argv for row, argv, _size in main._step_commands(job) if row.id == step.id)
    return main.step_config_digest(argv, record_to_job(job).env)


def _create_job_with_sources(db):
    job = JobRecord(
        name="backup",
        cron="",
        enabled=True,
        common_args="--exclude *.tmp",
        env_json='{"RCLONE_CONFIG_PASS": "x"}',
    )
    job.steps.append(JobStepRecord(position=1, name="Music", command="sync /media/a remote:a"))
    job.steps.append(JobStepRecord(position=2, name="List", command="lsd remote:"))
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


async def test_measure_route_queues_each_step_source(monkeypatch):
    fake = FakeMeasurer()
    monkeypatch.setattr(main, "measurer", fake)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)

            response = await main.measure_job_sources(job.id, None, None, db)

    assert response.headers["location"] == f"/jobs/{job.id}#step-sources"
    assert fake.started == [(["/media/a"], {"RCLONE_CONFIG_PASS": "x"})]


async def test_job_page_shows_source_measurements():
    from app.core.sources import size_command

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            argv = main.build_step_argv("sync /media/a remote:a", "--exclude *.tmp", {})
            key = size_command(argv, {"RCLONE_CONFIG_PASS": "x"}).key

            page = await main.job_detail(_request(f"/jobs/{job.id}"), job.id, None, db)
            html = page.body.decode()
            assert "Not measured" in html
            assert "No source to measure for this command." in html
            assert ">Measure sources</button>" in html

            db.add(
                SourceMeasurementRecord(
                    key=key,
                    source="/media/a",
                    status="failed",
                    bytes=3 * 1024**3,
                    files=38201,
                    started_at=main.utc_now(),
                    measured_at=main.utc_now(),
                    error="ERROR : directory not found",
                )
            )
            db.commit()
            html = (await main.job_detail(_request("/"), job.id, None, db)).body.decode()
            assert "3.000 GiB" in html
            assert "38,201" in html
            assert "Failed: ERROR : directory not found" in html
            assert 'data-measuring="false"' in html

            record = db.query(SourceMeasurementRecord).one()
            record.status = "running"
            db.commit()
            html = (await main.job_detail(_request("/"), job.id, None, db)).body.decode()
            assert '<span class="run-status running">Measuring</span>' in html
            assert 'data-measuring="true"' in html
            assert "disabled>Measuring…</button>" in html


async def test_expected_speed_setting_validates_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            saved = await main.save_estimate_settings(None, None, db, expected_speed=" 48,5 ")
            bad = await main.save_estimate_settings(None, None, db, expected_speed="fast")
            zero = await main.save_estimate_settings(None, None, db, expected_speed="0")
            page = await main.settings_page(_request("/settings"), None, db, speed_saved=1)

            assert saved.headers["location"] == "/settings?speed_saved=1#estimates"
            assert bad.headers["location"] == "/settings?speed_error=1#estimates"
            assert zero.headers["location"] == "/settings?speed_error=1#estimates"
            assert page.context["expected_speed"] == 48.5
            assert 'value="48.5"' in page.body.decode()

            cleared = await main.save_estimate_settings(None, None, db, expected_speed="")
            assert cleared.headers["location"] == "/settings?speed_saved=1#estimates"
            page = await main.settings_page(_request("/settings"), None, db)
            assert page.context["expected_speed"] is None


async def test_job_page_estimates_from_history_and_dry_runs():
    from app.core.rclone_stats import RcloneTransferStats, stats_to_json
    from app.core.sources import size_command

    gib = 1024**3
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            step_id = job.steps[0].id
            page = await main.job_detail(_request("/"), job.id, None, db)
            html = page.body.decode()
            assert "set an expected upload speed" in html
            assert "Needs a dry run" in html

            started = main.utc_now().replace(microsecond=0)
            from datetime import timedelta

            for trigger, transferred, seconds in (
                ("manual", 10 * gib, 1024),  # 10 MiB/s
                ("manual-dry-run", 3 * gib, 5),  # what the next sync would upload
            ):
                run = JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger=trigger,
                    status="success",
                    started_at=started,
                    ended_at=started + timedelta(seconds=seconds),
                )
                db.add(run)
                db.flush()
                db.add(
                    JobStepRunRecord(
                        job_run_id=run.id,
                        step_id=step_id,
                        config_digest=_step_digest(job, job.steps[0]),
                        step_name="Music",
                        argv_json="[]",
                        status="success",
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"{trigger}.log"),
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                        transfer_stats_json=stats_to_json(RcloneTransferStats(transferred, 100, 0)),
                    )
                )
            argv = main.build_step_argv("sync /media/a remote:a", "--exclude *.tmp", {})
            db.add(
                SourceMeasurementRecord(
                    key=size_command(argv, {"RCLONE_CONFIG_PASS": "x"}).key,
                    source="/media/a",
                    status="success",
                    bytes=36 * gib,
                    files=1000,
                    started_at=started,
                    measured_at=started,
                )
            )
            db.commit()

            page = await main.job_detail(_request("/"), job.id, None, db)
            html = page.body.decode()
            (music, _listing) = page.context["source_rows"]

    # 36 GiB at 10 MiB/s is an hour; the dry run's 3 GiB takes just over 5 minutes.
    assert music["full_estimate"] == "about 1 h"
    assert music["next_estimate"] == "about 6 min"
    assert "3.000 GiB to upload" in html
    assert page.context["source_speed"].explanation == "this job's last sizeable run"
    assert "Estimates assume 10.0 MiB/s, this job&#39;s last sizeable run." in html


async def test_estimates_cap_each_step_by_its_own_bwlimit():
    from app.core.estimates import save_expected_speed
    from app.core.sources import size_command

    gib = 1024**3
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(
                JobStepRecord(position=1, name="Fast", command="sync /a r:a --bwlimit 100M")
            )
            job.steps.append(
                JobStepRecord(position=2, name="Slow", command="sync /b r:b --bwlimit 1M")
            )
            db.add(job)
            save_expected_speed(db, 100)
            for step in job.steps:
                argv = main.build_step_argv(step.command, "", {})
                db.add(
                    SourceMeasurementRecord(
                        key=size_command(argv).key,
                        source="x",
                        status="success",
                        bytes=36 * gib,
                        files=10,
                        started_at=main.utc_now(),
                        measured_at=main.utc_now(),
                    )
                )
            db.commit()

            context = main._sources_context(job, db)

    fast, slow = context["source_rows"]
    # 36 GiB at 100 MiB/s is about 6 minutes; at 1 MiB/s it is about 10 hours.
    assert fast["full_estimate"] == "about 7 min"
    assert fast["speed_note"] is None
    assert slow["full_estimate"] == "about 10 h 15 min"
    assert "--bwlimit" in slow["speed_note"]
    assert context["source_totals"]["estimate"] == "about 10 h 20 min"


async def test_runs_with_dry_run_flags_count_as_dry_runs_not_speed_samples():
    from datetime import timedelta

    from app.core.rclone_stats import RcloneTransferStats, stats_to_json

    gib = 1024**3
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            started = main.utc_now()
            for argv in (["rclone", "sync", "-n", "/media/a", "remote:a"],):
                run = JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger="schedule",
                    status="success",
                    started_at=started,
                    ended_at=started + timedelta(seconds=600),
                )
                db.add(run)
                db.flush()
                db.add(
                    JobStepRunRecord(
                        job_run_id=run.id,
                        step_id=job.steps[0].id,
                        config_digest=_step_digest(job, job.steps[0]),
                        step_name="Music",
                        argv_json=json.dumps(argv),
                        status="success",
                        exit_code=0,
                        log_path=str(Path(tmpdir) / "run.log"),
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                        transfer_stats_json=stats_to_json(RcloneTransferStats(50 * gib, 10, 0)),
                    )
                )
            db.commit()

            samples = main._upload_samples(job, db)
            dry_runs = main._latest_dry_run_stats(job, db)

    assert samples == []
    assert dry_runs[job.steps[0].id]["bytes"] == 50 * gib


def test_dry_run_detection_reads_the_command():
    from types import SimpleNamespace

    def run(trigger, argv):
        return SimpleNamespace(job_run=SimpleNamespace(trigger=trigger), argv_json=json.dumps(argv))

    assert main._is_dry_run(run("manual-dry-run", ["rclone", "sync"]))
    assert main._is_dry_run(run("schedule", ["rclone", "sync", "--dry-run", "a", "b"]))
    assert main._is_dry_run(run("schedule", ["rclone", "sync", "--dry-run=true", "a", "b"]))
    assert not main._is_dry_run(run("schedule", ["rclone", "sync", "--dry-run=false", "a", "b"]))
    assert not main._is_dry_run(run("schedule", ["rclone", "sync", "a", "b"]))
    # After "--", "-n" is a path.
    assert not main._is_dry_run(run("schedule", ["rclone", "copy", "--", "-n", "b"]))
    # rclone uses the last value, and -n combines with other short flags.
    assert main._is_dry_run(run("schedule", ["rclone", "sync", "--dry-run=false", "-n", "a", "b"]))
    assert not main._is_dry_run(
        run("schedule", ["rclone", "sync", "--dry-run", "--dry-run=false", "a", "b"])
    )
    assert main._is_dry_run(run("schedule", ["rclone", "sync", "-vn", "a", "b"]))
    # An explicit value on the short flag counts too, as in rclone.
    assert not main._is_dry_run(run("schedule", ["rclone", "copy", "a", "b", "-n=false"]))
    assert not main._is_dry_run(run("schedule", ["rclone", "copy", "a", "b", "-vn=false"]))
    assert main._is_dry_run(run("schedule", ["rclone", "copy", "a", "b", "-n=true"]))
    assert main._is_dry_run(run("schedule", ["rclone", "copy", "a", "b", "-nv=false"]))
    assert not main._is_dry_run(run("schedule", ["rclone", "sync", "-vv", "a", "b"]))


async def test_a_dry_run_stays_found_behind_many_ordinary_runs():
    from datetime import timedelta

    from app.core.rclone_stats import RcloneTransferStats, stats_to_json

    gib = 1024**3
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            step_id = job.steps[0].id
            start = main.utc_now() - timedelta(days=300)
            for index in range(251):
                trigger = "manual-dry-run" if index == 0 else "schedule"
                run = JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger=trigger,
                    status="success",
                    started_at=start + timedelta(hours=index),
                    ended_at=start + timedelta(hours=index, minutes=10),
                )
                db.add(run)
                db.flush()
                db.add(
                    JobStepRunRecord(
                        job_run_id=run.id,
                        step_id=step_id,
                        config_digest=_step_digest(job, job.steps[0]),
                        step_name="Music",
                        argv_json="[]",
                        status="success",
                        exit_code=0,
                        log_path=str(Path(tmpdir) / f"{index}.log"),
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                        transfer_stats_json=stats_to_json(
                            RcloneTransferStats(2 * gib if index else 7 * gib, 1, 0)
                        ),
                    )
                )
            db.commit()

            dry_runs = main._latest_dry_run_stats(job, db)
            samples = main._upload_samples(job, db)

    assert dry_runs[step_id]["bytes"] == 7 * gib
    # The search stops once it has enough sizeable real runs.
    assert len(samples) == 10


async def test_job_page_says_when_rclone_flags_are_unavailable(monkeypatch):
    import app.core.sources as sources

    async def no_load():
        return None

    monkeypatch.setattr(sources, "_flag_sets", {})
    monkeypatch.setattr(main, "ensure_flags", no_load)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            page = await main.job_detail(_request("/"), job.id, None, db)

    assert "can't be identified right now" in page.body.decode()


def test_dry_run_detection_reads_prune_steps():
    from types import SimpleNamespace

    def run(trigger, argv):
        return SimpleNamespace(job_run=SimpleNamespace(trigger=trigger), argv_json=json.dumps(argv))

    argv = ["prune-dated", "crypt:v", "--keep-days", "30", "--keep-min", "7"]
    assert not main._is_dry_run(run("schedule", argv))
    assert main._is_dry_run(run("schedule", [*argv, "--dry-run"]))
    assert main._is_dry_run(run("manual-step-dry-run", argv))
    assert not main._is_dry_run(run("schedule", [*argv, "--exclude", "Thumbnails"]))


async def test_job_editor_marks_prune_steps_as_destructive():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(JobStepRecord(position=1, name="Music", command="sync /a crypt:a"))
            job.steps.append(
                JobStepRecord(
                    position=2,
                    name="Prune",
                    command="prune-dated crypt:versions --keep-days 30 --keep-min 7",
                )
            )
            job.steps.append(
                JobStepRecord(
                    position=3,
                    name="Trial",
                    command="prune-dated crypt:old --keep-days 1 --keep-min 1 --dry-run",
                )
            )
            job.steps.append(
                JobStepRecord(
                    position=4, name="Root", command="prune-dated crypt: --keep-days 1 --keep-min 1"
                )
            )
            db.add(job)
            db.commit()

            page = await main.job_detail(_request("/"), job.id, None, db)
            html = page.body.decode()
            response = await main.jobs(_request("/jobs"), None, db)
            jobs_html = response.body.decode()

    deletes = (
        "permanently deletes dated folders in crypt:versions older than 30 days, "
        "always keeping the newest 7"
    )
    previews = {preview["name"]: preview for preview in page.context["command_previews"]}
    assert previews["Prune"]["deletes"] == deletes
    # A step that is always a dry run deletes nothing; neither does one that can't run.
    assert previews["Trial"]["deletes"] is None
    assert previews["Root"]["deletes"] is None
    assert previews["Root"]["command"] == (
        "Invalid command: prune-dated: refusing to prune the root of crypt:"
    )
    assert html.count('<span class="badge destructive">Deletes folders</span>') == 1
    assert f"This step {deletes}." in html

    step_confirm = f"Run step Prune now? This is a real run, not a dry run: it {deletes}."
    assert f"return confirm({json.dumps(step_confirm)})" in html
    assert 'return confirm("Run step Music now? This is a real run, not a dry run.")' in html
    run_confirm = (
        "Run backup now? This is a real run, not a dry run: sync can delete files at the "
        f"destination. Step Prune {deletes}."
    )
    assert page.context["run_warning"] == f" Step Prune {deletes}."
    assert response.context["jobs"][0]["run_warning"] == f" Step Prune {deletes}."
    for page_html in (html, jobs_html):
        assert f"return confirm({json.dumps(run_confirm)})" in page_html


async def test_saved_job_with_refused_schedule_says_it_is_not_scheduled():
    # Saved before day-of-month plus weekday schedules were refused: it is no longer run on
    # a schedule, so the job pages must say why.
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = JobRecord(
                name="backup", cron="0 2 1 * 1", enabled=True, common_args="", env_json="{}"
            )
            db.add(job)
            db.commit()

            listing = await main.jobs(_request("/jobs"), None, db)
            detail = await main.job_detail(_request(f"/jobs/{job.id}"), job.id, None, db)

            assert "not both" in listing.body.decode()
            assert "Not scheduled" in listing.body.decode()
            assert "This job is not scheduled until its schedule is fixed." in detail.body.decode()


@pytest.mark.parametrize("cron", ["0 2 * * 7/1", "0 2 1 * 1", "0 2 * * 8"])
async def test_job_forms_report_refused_weekday_schedules(monkeypatch, cron):
    monkeypatch.setattr(main, "sync_schedules", lambda db: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            response = await main.create_job(
                _request("/jobs", "POST"), None, None, db, **_job_form(cron=cron)
            )
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            db.add(job)
            db.commit()
            update = await main.update_job(
                _request(f"/jobs/{job.id}", "POST"), job.id, None, None, db, **_job_form(cron=cron)
            )

    for page in (response, update):
        assert page.status_code == 400
        assert "Invalid cron schedule" in page.body.decode()


async def test_log_retention_keeps_logs_of_runs_in_progress():
    from datetime import timedelta

    from app.core.retention import prune_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        session_factory = _session_factory(root / "runs.db")
        long_ago = main.utc_now() - timedelta(days=2)
        logs = {name: root / f"{name}.log" for name in ("active", "earlier", "done", "console")}
        logs["old_console"] = root / "old_console.log"
        for path in logs.values():
            path.write_text("output", encoding="utf-8")
        with session_factory() as db:
            running = JobRunRecord(
                job_id=1,
                job_name="slow",
                trigger="manual",
                status="running",
                started_at=long_ago,
                ended_at=None,
            )
            finished = JobRunRecord(
                job_id=2,
                job_name="quick",
                trigger="manual",
                status="success",
                started_at=long_ago,
                ended_at=long_ago,
            )
            db.add_all([running, finished])
            db.flush()

            def step_run(job_run, log, status):
                ended_at = None if status == "running" else long_ago
                return JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=None,
                    step_name=log,
                    argv_json="[]",
                    status=status,
                    exit_code=0 if ended_at else None,
                    log_path=str(logs[log]),
                    started_at=long_ago,
                    ended_at=ended_at,
                )

            db.add_all(
                [
                    # A step still running, and one that finished earlier in the same run.
                    step_run(running, "active", "running"),
                    step_run(running, "earlier", "success"),
                    step_run(finished, "done", "success"),
                    ConsoleRunRecord(
                        status="running",
                        command="ncdu",
                        argv_json="[]",
                        exit_code=None,
                        log_path=str(logs["console"]),
                        started_at=long_ago,
                        ended_at=None,
                    ),
                    ConsoleRunRecord(
                        status="success",
                        command="lsd r:",
                        argv_json="[]",
                        exit_code=0,
                        log_path=str(logs["old_console"]),
                        started_at=long_ago,
                        ended_at=long_ago,
                    ),
                ]
            )
            db.commit()

            deleted = prune_logs(main._prunable_logs(db), keep_days=1)

        assert deleted == 2
        assert logs["active"].exists()
        assert logs["earlier"].exists()
        assert logs["console"].exists()
        assert not logs["done"].exists()
        assert not logs["old_console"].exists()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
async def test_log_retention_keeps_logs_of_recovered_runs_whose_process_lives_on(monkeypatch):
    import subprocess
    import sys
    from datetime import timedelta

    monkeypatch.setattr(settings, "retention_days", 1)
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        log_path = Path(tmpdir) / "step.log"
        with log_path.open("wb") as output:
            # An rclone that outlived the app: it still has the log open.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], stdout=output
            )
        try:
            with session_factory() as db:
                step_run = _create_running_step_run(db, log_path=log_path)
                step_run.started_at = step_run.job_run.started_at = main.utc_now() - timedelta(
                    days=2
                )
                db.commit()
            # The app restarts: the run is closed out as failed, but its process lives on.
            LiveJobRunner(Path(tmpdir), session_factory=session_factory).recover_interrupted_runs()
            with session_factory() as db:
                assert db.get(JobStepRunRecord, step_run.id).status == "failed"
                response = await main.prune(None, None, db)
            assert response.headers["location"] == "/settings?pruned=0"
            assert log_path.exists()
        finally:
            child.kill()
            child.wait()
        with session_factory() as db:
            response = await main.prune(None, None, db)
        assert response.headers["location"] == "/settings?pruned=1"
        assert not log_path.exists()


async def test_a_dry_run_without_stats_gives_no_next_sync_estimate():
    from app.core.estimates import save_expected_speed

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job_with_sources(db)
            save_expected_speed(db, 10)
            started = main.utc_now()
            run = JobRunRecord(
                job_id=job.id,
                job_name=job.name,
                trigger="manual-dry-run",
                status="success",
                started_at=started,
                ended_at=started,
            )
            db.add(run)
            db.flush()
            db.add(
                JobStepRunRecord(
                    job_run_id=run.id,
                    step_id=job.steps[0].id,
                    config_digest=_step_digest(job, job.steps[0]),
                    step_name="Music",
                    argv_json="[]",
                    status="success",
                    exit_code=0,
                    log_path=str(Path(tmpdir) / "dry.log"),
                    started_at=started,
                    ended_at=started,
                    transfer_stats_json=STATS_UNAVAILABLE_JSON,
                )
            )
            db.commit()

            page = await main.job_detail(_request("/"), job.id, None, db)
            html = page.body.decode()
            (music, _listing) = page.context["source_rows"]

    # Not "No changes" and not an estimate of zero: the dry run's log had no totals.
    assert music["dry_run"]["bytes"] is None
    assert music["next_estimate"] is None
    assert "Upload size not available from the dry run of" in html
    assert "No changes" not in html


def test_history_totals_say_when_some_steps_had_no_stats():
    assert main._transfer_summary(2048, 1, 0, "success", partial=True) == {
        "data": "2 KiB",
        "detail": "1 file; some steps not available",
    }
    assert main._transfer_summary(0, 0, 0, "success", partial=True) == {
        "data": "0 B",
        "detail": "0 files; some steps not available",
    }
    assert main._unavailable_transfer_summary("success") == {
        "data": "Not available",
        "detail": None,
    }
    assert main._unavailable_transfer_summary("failed") == {"data": "-", "detail": None}


async def test_a_run_in_progress_does_not_report_its_totals_as_exact():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            # The first step is still transferring: nothing is known yet.
            step_run = _create_running_step_run(db, log_path=Path(tmpdir) / "one.log")
            job_run = step_run.job_run
            payload = main._job_run_status_payload(job_run, db)
            assert payload["stats"] == {
                "transferred_data": "Pending",
                "transferred_files": "Pending",
                "deleted_files": "Pending",
            }
            page = await main.job_run_detail(_request("/"), job_run.id, None, db)
            assert "0 B" not in page.body.decode()

            # A finished step's totals are known, but the running one's are still to come.
            step_run.status = "success"
            step_run.exit_code = 0
            step_run.ended_at = main.utc_now()
            step_run.transfer_stats_json = stats_to_json(RcloneTransferStats(2048, 1, 0))
            db.add(
                JobStepRunRecord(
                    job_run_id=job_run.id,
                    step_id=2,
                    step_name="two",
                    argv_json="[]",
                    status="running",
                    exit_code=None,
                    log_path=str(Path(tmpdir) / "two.log"),
                    started_at=main.utc_now(),
                    ended_at=None,
                )
            )
            db.commit()
            db.refresh(job_run)
            payload = main._job_run_status_payload(job_run, db)

    assert payload["stats"] == {
        "transferred_data": "at least 2 KiB",
        "transferred_files": "at least 1",
        "deleted_files": "at least 0",
    }


async def test_estimates_are_capped_by_a_bwlimit_from_the_jobs_environment():
    from app.core.estimates import save_expected_speed
    from app.core.sources import size_command

    gib = 1024**3
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            env = {"RCLONE_BWLIMIT": "1M"}
            job = JobRecord(
                name="backup", cron="", enabled=True, common_args="", env_json=json.dumps(env)
            )
            job.steps.append(JobStepRecord(position=1, name="Slow", command="sync /b r:b"))
            db.add(job)
            save_expected_speed(db, 100)
            argv = main.build_step_argv("sync /b r:b", "", {})
            db.add(
                SourceMeasurementRecord(
                    key=size_command(argv, env).key,
                    source="/b",
                    status="success",
                    bytes=36 * gib,
                    files=10,
                    started_at=main.utc_now(),
                    measured_at=main.utc_now(),
                )
            )
            db.commit()

            (slow,) = main._sources_context(job, db)["source_rows"]

    # rclone reads RCLONE_BWLIMIT like --bwlimit: 36 GiB at 1 MiB/s is about 10 hours.
    assert slow["full_estimate"] == "about 10 h 15 min"


DRY_RUN_TOTALS = (
    "Transferred:   \t   123 B / 123 B, 100%, 0 B/s, ETA -\nTransferred:            1 / 1, 100%\n"
)


@pytest.mark.parametrize(
    ("change", "kept"),
    [
        (lambda job: _set_command(job, "copy", "copy /different /elsewhere"), False),
        (lambda job: setattr(job, "common_args", "--exclude '*.tmp'"), False),
        (
            lambda job: setattr(job, "env_json", json.dumps({"RCLONE_CONFIG_DEST_TYPE": "s3"})),
            False,
        ),
        # Another rclone.conf may give the same remote name another meaning.
        (lambda job: setattr(job, "env_json", json.dumps({"XDG_CONFIG_HOME": "/other"})), False),
        # How fast it runs says nothing about what it would transfer.
        (lambda job: setattr(job, "env_json", json.dumps({"RCLONE_BWLIMIT": "1M"})), True),
        # Another step's edit says nothing about this one's next sync.
        (lambda job: _set_command(job, "list", "lsf /other"), True),
    ],
)
async def test_next_sync_estimates_come_only_from_dry_runs_of_the_current_configuration(
    change, kept
):
    from app.core.estimates import save_expected_speed

    async def executor(argv, env, log_path):
        log_path.write_text(DRY_RUN_TOTALS, encoding="utf-8")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(JobStepRecord(position=1, name="copy", command="copy /old /dest"))
            job.steps.append(JobStepRecord(position=2, name="list", command="lsf /dest"))
            db.add(job)
            save_expected_speed(db, 10)
            db.commit()
            first = runner.start_job(record_to_job(job), trigger="manual-dry-run", dry_run=True)
            await runner.wait_for_job_run(first.job_run_id)

            db.expire_all()
            (copy, _listing) = main._sources_context(job, db)["source_rows"]
            assert copy["dry_run"]["bytes"] == 123

            # Edit the job the way the job form does: step names keep their IDs.
            change(job)
            db.commit()
            db.expire_all()
            (copy, _listing) = main._sources_context(job, db)["source_rows"]

    if kept:
        assert copy["dry_run"]["bytes"] == 123
    else:
        # The run history stays; only the estimate goes.
        assert copy["dry_run"] is None
        assert copy["next_estimate"] is None


def _set_command(job, name, command):
    steps_text = "\n".join(
        f"{step.name}|{command if step.name == name else step.command}"
        for step in sorted(job.steps, key=lambda step: step.position)
    )
    from sqlalchemy.orm import object_session

    main._replace_steps(object_session(job), job, steps_text)


@pytest.mark.parametrize(
    ("command", "kept"),
    [
        # A new day is a new folder to copy to: what the dry run measured is gone.
        ("copy /source/${RUN_DATE} /dest/${RUN_DATE}", False),
        # A dated --backup-dir doesn't change what the next sync uploads.
        ("copy /source /dest --backup-dir /versions/${RUN_DATE}", True),
    ],
)
async def test_next_sync_estimates_follow_paths_that_change_with_the_run_date(
    monkeypatch, command, kept
):
    from datetime import UTC, datetime

    import app.core.runner as runner_module

    async def executor(argv, env, log_path):
        log_path.write_text(DRY_RUN_TOTALS, encoding="utf-8")
        return 0

    day = [datetime(2026, 11, 15, 12, tzinfo=UTC)]
    monkeypatch.setattr(runner_module, "utc_now", lambda: day[0])
    monkeypatch.setattr(main, "utc_now", lambda: day[0])
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(JobStepRecord(position=1, name="copy", command=command))
            db.add(job)
            db.commit()
            first = runner.start_job(record_to_job(job), trigger="manual-dry-run", dry_run=True)
            await runner.wait_for_job_run(first.job_run_id)
            db.expire_all()
            assert main._sources_context(job, db)["source_rows"][0]["dry_run"]["bytes"] == 123

            day[0] = datetime(2026, 11, 16, 12, tzinfo=UTC)
            db.expire_all()
            (row,) = main._sources_context(job, db)["source_rows"]

    assert (row["dry_run"] is not None) is kept


async def test_a_dry_run_counts_for_the_step_without_its_dry_run_flag():
    # Taking --dry-run out of a step to make it real changes nothing it would transfer.
    async def executor(argv, env, log_path):
        log_path.write_text(DRY_RUN_TOTALS, encoding="utf-8")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        runner = LiveJobRunner(Path(tmpdir), session_factory=session_factory, executor=executor)
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(
                JobStepRecord(position=1, name="copy", command="copy /old /dest --dry-run -v")
            )
            db.add(job)
            db.commit()
            first = runner.start_job(record_to_job(job), trigger="manual")
            await runner.wait_for_job_run(first.job_run_id)

            _set_command(job, "copy", "copy /old /dest")
            db.commit()
            db.expire_all()
            (row,) = main._sources_context(job, db)["source_rows"]

    assert row["dry_run"]["bytes"] == 123


async def test_dry_runs_saved_by_the_simple_runner_give_estimates():
    from app.core.runner import JobRunner
    from app.db import save_job_run

    async def executor(argv, env, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(DRY_RUN_TOTALS, encoding="utf-8")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = JobRecord(name="backup", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(JobStepRecord(position=1, name="copy", command="copy /old /dest"))
            db.add(job)
            db.commit()
            result = await JobRunner(Path(tmpdir), executor=executor).run_job(
                record_to_job(job), trigger="manual-dry-run", dry_run=True
            )
            save_job_run(db, result)
            (row,) = main._sources_context(job, db)["source_rows"]

    assert row["dry_run"]["bytes"] == 123


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the rclone binary")
async def test_a_dry_run_with_a_backup_dir_says_nothing_of_one_without():
    # Replacing a file moves the old one aside, which rclone counts: 3 B to copy plus 9 B to
    # move make 12 B with --backup-dir, 3 B without it.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "src").mkdir()
        (root / "dst").mkdir()
        (root / "src" / "a").write_text("abc", encoding="utf-8")
        (root / "dst" / "a").write_text("123456789", encoding="utf-8")
        config = root / "rclone.conf"
        config.touch()
        session_factory = _session_factory(root / "runs.db")
        runner = LiveJobRunner(root / "logs", session_factory=session_factory)
        with session_factory() as db:
            job = JobRecord(
                name="backup",
                cron="",
                enabled=True,
                common_args=f"--config {config}",
                env_json="{}",
            )
            job.steps.append(
                JobStepRecord(
                    position=1,
                    name="copy",
                    command=f"copy {root / 'src'} {root / 'dst'} --backup-dir {root / 'old'}",
                )
            )
            db.add(job)
            db.commit()

            async def dry_run():
                first = runner.start_job(record_to_job(job), trigger="manual-dry-run", dry_run=True)
                await runner.wait_for_job_run(first.job_run_id)
                db.expire_all()
                return main._sources_context(job, db)["source_rows"][0]["dry_run"]

            assert (await dry_run())["bytes"] == 12
            _set_command(job, "copy", f"copy {root / 'src'} {root / 'dst'}")
            db.commit()
            db.expire_all()
            assert main._sources_context(job, db)["source_rows"][0]["dry_run"] is None
            assert (await dry_run())["bytes"] == 3


async def test_job_pages_disable_dry_runs_of_commands_that_ignore_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = JobRecord(name="tidy", cron="", enabled=True, common_args="", env_json="{}")
            job.steps.append(JobStepRecord(position=1, name="List", command="lsd remote:"))
            job.steps.append(
                JobStepRecord(position=2, name="Forget", command="config delete remote")
            )
            db.add(job)
            db.commit()

            page = await main.job_detail(_request(f"/jobs/{job.id}"), job.id, None, db)
            html = page.body.decode()
            listing = await main.jobs(_request("/jobs"), None, db)

    reason = "A dry run can&#39;t start: rclone config delete can make changes even with --dry-run"
    previews = {preview["name"]: preview for preview in page.context["command_previews"]}
    assert previews["List"]["dry_run_refusal"] is None
    assert previews["Forget"]["dry_run_refusal"] == reason.replace("&#39;", "'")
    assert f'disabled title="{reason}">Dry run step</button>' in html
    assert html.count("disabled title=") == 2  # The step's button and the job's.
    assert f'disabled title="Step Forget: {reason}.">Dry run</button>' in html
    assert listing.context["jobs"][0]["dry_run_refusal"] == (
        f"Step Forget: {reason.replace('&#39;', chr(39))}."
    )

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect

from app.auth import SESSION_KEY, AuthRequired, csrf_field, require_csrf, verify_password
from app.config import runtime_warnings, settings
from app.core.commands import CommandPolicyError, build_rclone_argv, parse_console_command
from app.core.logs import read_log_append, read_log_chunk
from app.core.models import utc_now
from app.core.notifications import (
    EmailNotificationSettings,
    load_email_notification_settings,
    save_email_notification_settings,
    send_test_notification,
)
from app.core.pty_console import bridge_pty, display_argv
from app.core.rclone_stats import (
    RcloneTransferStats,
    RunStatsDisplay,
    pending_stats_display,
    run_stats_display,
    stats_from_json,
    stats_from_log,
    step_stats_display,
)
from app.core.retention import prune_logs
from app.core.schedule import cron_summary, normalize_cron
from app.db import (
    ConsoleRunRecord,
    DbSession,
    JobRecord,
    JobRunRecord,
    JobStepRecord,
    JobStepRunRecord,
    SessionLocal,
    env_to_lines,
    init_db,
    parse_env_lines,
    record_to_job,
)
from app.runner_service import runner
from app.scheduler import next_run_time, scheduler, sync_schedules


def inject_runtime_warnings(request: Request) -> dict[str, object]:
    return {
        "runtime_warnings": runtime_warnings(settings),
        "csrf_field": lambda: csrf_field(request),
    }


templates = Jinja2Templates(directory="app/templates", context_processors=[inject_runtime_warnings])


def run_mode_label(trigger: str) -> str:
    if "dry-run" in trigger:
        return "Dry run"
    if trigger == "schedule":
        return "Scheduled"
    if "step" in trigger:
        return "Step run"
    return "Run"


templates.env.globals["run_mode_label"] = run_mode_label
templates.env.globals["format_local_time"] = lambda value: _format_local_time(value)
templates.env.globals["format_duration"] = lambda start, end: _format_duration(start, end)
templates.env.globals["utc_now"] = utc_now
templates.env.globals["run_exit_label"] = lambda run: _exit_label(run.status, run.exit_code)


LOG_CHUNK_LINES = 200
HISTORY_PAGE_SIZE = 25


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    with next_db() as session:
        sync_schedules(session)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="Rclone Runner", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    return app


app = create_app()


def next_db():
    from app.db import SessionLocal

    return SessionLocal()


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(
    request: Request,
    _csrf: Annotated[None, Depends(require_csrf)],
    username: str = Form(),
    password: str = Form(),
) -> Response:
    if username == settings.admin_user and verify_password(password):
        request.session[SESSION_KEY] = True
        return RedirectResponse("/jobs", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid credentials"}, status_code=401
    )


@app.post("/logout")
async def logout(
    request: Request,
    _csrf: Annotated[None, Depends(require_csrf)],
) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(_: AuthRequired) -> Response:
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, _: AuthRequired, db: DbSession, deleted: bool = False) -> Response:
    records = db.query(JobRecord).order_by(JobRecord.name).all()
    ongoing_job_runs = (
        db.query(JobRunRecord).filter_by(status="running").order_by(JobRunRecord.started_at).all()
    )
    ongoing_step_runs = (
        db.query(JobStepRunRecord)
        .filter_by(status="running")
        .order_by(JobStepRunRecord.started_at)
        .all()
    )
    ongoing_console_runs = (
        db.query(ConsoleRunRecord)
        .filter_by(status="running")
        .order_by(ConsoleRunRecord.started_at)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": _job_rows(records, db),
            "ongoing_job_runs": _ongoing_job_run_rows(ongoing_job_runs),
            "ongoing_step_runs": ongoing_step_runs,
            "ongoing_console_runs": ongoing_console_runs,
            "deleted": deleted,
        },
    )


@app.get("/jobs/new", response_class=HTMLResponse)
async def new_job(request: Request, _: AuthRequired) -> Response:
    return templates.TemplateResponse(
        request,
        "job_form.html",
        {
            "job": None,
            "schedule_summary": cron_summary(""),
            "env_lines": "",
            "steps_text": "",
            "command_previews": [],
        },
    )


@app.post("/jobs")
async def create_job(
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
    name: str = Form(),
    cron: str = Form(""),
    common_args: str = Form(""),
    env_lines: str = Form(""),
    steps_text: str = Form(""),
    enabled: str | None = Form(None),
) -> Response:
    job = JobRecord(
        name=name,
        cron=normalize_cron(cron),
        enabled=enabled == "on",
        common_args=common_args,
        env_json=json.dumps(parse_env_lines(env_lines)),
    )
    db.add(job)
    db.flush()
    _replace_steps(job, steps_text)
    db.commit()
    sync_schedules(db)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request, job_id: int, _: AuthRequired, db: DbSession, delete_blocked: bool = False
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    job_runs = (
        db.query(JobRunRecord)
        .filter_by(job_id=job_id)
        .order_by(JobRunRecord.started_at.desc())
        .limit(20)
        .all()
    )
    runs = (
        db.query(JobStepRunRecord)
        .join(JobStepRunRecord.job_run)
        .filter_by(job_id=job_id)
        .order_by(JobStepRunRecord.started_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "schedule_summary": cron_summary(job.cron),
            "env_lines": env_to_lines(job.env_json),
            "steps_text": _steps_to_text(job.steps),
            "command_previews": _command_previews(job),
            "job_runs": job_runs,
            "runs": runs,
            "delete_blocked": delete_blocked,
        },
    )


@app.post("/jobs/{job_id}")
async def update_job(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
    name: str = Form(),
    cron: str = Form(""),
    common_args: str = Form(""),
    env_lines: str = Form(""),
    steps_text: str = Form(""),
    enabled: str | None = Form(None),
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    job.name = name
    job.cron = normalize_cron(cron)
    job.common_args = common_args
    job.env_json = json.dumps(parse_env_lines(env_lines))
    job.enabled = enabled == "on"
    job.updated_at = utc_now()
    _replace_steps(job, steps_text)
    db.commit()
    sync_schedules(db)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/delete")
async def delete_job(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    running = db.query(JobRunRecord).filter_by(job_id=job_id, status="running").first()
    if running is not None:
        return RedirectResponse(f"/jobs/{job_id}?delete_blocked=1", status_code=303)
    _delete_job_configuration(db, job)
    db.commit()
    sync_schedules(db)
    return RedirectResponse("/jobs?deleted=1", status_code=303)


@app.post("/jobs/{job_id}/run")
async def run_job(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    return await _run_job(job_id, "manual", False, db)


@app.post("/jobs/{job_id}/dry-run")
async def dry_run_job(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    return await _run_job(job_id, "manual-dry-run", True, db)


@app.post("/jobs/{job_id}/steps/{step_id}/run")
async def run_job_step(
    job_id: int,
    step_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    return await _run_job(job_id, "manual-step", False, db, step_id=step_id)


@app.post("/jobs/{job_id}/steps/{step_id}/dry-run")
async def dry_run_job_step(
    job_id: int,
    step_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    return await _run_job(job_id, "manual-step-dry-run", True, db, step_id=step_id)


async def _run_job(
    job_id: int, trigger: str, dry_run: bool, db: DbSession, step_id: int | None = None
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    first_step_run = runner.start_job(
        record_to_job(job), trigger=trigger, dry_run=dry_run, step_id=step_id
    )
    if first_step_run is None:
        return RedirectResponse("/runs", status_code=303)
    if step_id is None:
        return RedirectResponse(f"/job-runs/{first_step_run.job_run_id}", status_code=303)
    return RedirectResponse(f"/runs/{first_step_run.id}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
async def runs(
    request: Request,
    _: AuthRequired,
    db: DbSession,
    job_page: int = 1,
    step_page: int = 1,
    console_page: int = 1,
) -> Response:
    job_query = db.query(JobRunRecord).order_by(JobRunRecord.started_at.desc())
    step_query = db.query(JobStepRunRecord).order_by(JobStepRunRecord.started_at.desc())
    console_query = db.query(ConsoleRunRecord).order_by(ConsoleRunRecord.started_at.desc())
    job_runs, job_pagination = _paginated_history(
        job_query,
        page=job_page,
        page_param="job_page",
        other_pages={"step_page": step_page, "console_page": console_page},
    )
    recent, step_pagination = _paginated_history(
        step_query,
        page=step_page,
        page_param="step_page",
        other_pages={"job_page": job_page, "console_page": console_page},
    )
    console_runs, console_pagination = _paginated_history(
        console_query,
        page=console_page,
        page_param="console_page",
        other_pages={"job_page": job_page, "step_page": step_page},
    )
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "job_runs": job_runs,
            "step_runs": recent,
            "console_runs": console_runs,
            "job_pagination": job_pagination,
            "step_pagination": step_pagination,
            "console_pagination": console_pagination,
        },
    )


@app.get("/job-runs/{job_run_id}", response_class=HTMLResponse)
async def job_run_detail(
    request: Request, job_run_id: int, _: AuthRequired, db: DbSession
) -> Response:
    job_run = db.get(JobRunRecord, job_run_id)
    if job_run is None:
        return RedirectResponse("/runs", status_code=303)
    active_step_run = _active_step_run(job_run)
    context = {
        "job_run": job_run,
        "step_rows": _job_run_step_rows(job_run, db),
        "run_stats": _job_run_stats(job_run),
        "active_step_run": active_step_run,
        "job_run_status_url": f"/job-runs/{job_run.id}/status",
        "job_run_cancel_url": f"/job-runs/{job_run.id}/cancel",
    }
    if active_step_run is not None:
        log_path = Path(active_step_run.log_path)
        context |= {
            "run": active_step_run,
            "argv": json.loads(active_step_run.argv_json),
            "log_chunk": read_log_chunk(log_path, limit=LOG_CHUNK_LINES),
            "log_chunk_url": f"/runs/{active_step_run.id}/log/chunk",
            "log_append_url": f"/runs/{active_step_run.id}/log/append",
            "log_status_url": f"/runs/{active_step_run.id}/status",
            "log_append_offset": _log_size(log_path),
            "raw_log_url": f"/runs/{active_step_run.id}/log/raw",
        }
    return templates.TemplateResponse(request, "job_run_detail.html", context)


@app.get("/job-runs/{job_run_id}/status")
async def job_run_status(job_run_id: int, _: AuthRequired, db: DbSession) -> dict[str, object]:
    job_run = db.get(JobRunRecord, job_run_id)
    if job_run is None:
        return {"status": "missing", "can_cancel": False, "steps": []}
    return _job_run_status_payload(job_run, db)


@app.post("/job-runs/{job_run_id}/cancel")
async def cancel_job_run(
    job_run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    if not runner.cancel_job_run(job_run_id):
        _mark_job_run_canceled(db, job_run_id)
    return RedirectResponse(f"/job-runs/{job_run_id}", status_code=303)


@app.post("/job-runs/{job_run_id}/delete")
async def delete_job_run(
    job_run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    job_run = db.get(JobRunRecord, job_run_id)
    if job_run is not None:
        _delete_job_run(db, job_run)
    return RedirectResponse("/runs", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int, _: AuthRequired, db: DbSession) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return RedirectResponse("/runs", status_code=303)
    log_chunk = read_log_chunk(Path(run.log_path), limit=LOG_CHUNK_LINES)
    log_size = _log_size(Path(run.log_path))
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "argv": json.loads(run.argv_json),
            "step_stats": _step_stats_display_for_run(run),
            "job_run_step_count": len(run.job_run.step_runs),
            "log_chunk": log_chunk,
            "log_chunk_url": f"/runs/{run.id}/log/chunk",
            "log_append_url": f"/runs/{run.id}/log/append",
            "log_status_url": f"/runs/{run.id}/status",
            "log_append_offset": log_size,
            "raw_log_url": f"/runs/{run.id}/log/raw",
        },
    )


@app.get("/runs/{run_id}/log/chunk")
async def run_log_chunk(
    run_id: int, _: AuthRequired, db: DbSession, before: int | None = None
) -> dict[str, object]:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return {"text": "", "next_before": None, "has_more": False}
    return _log_chunk_payload(Path(run.log_path), before)


@app.get("/runs/{run_id}/status")
async def run_status(run_id: int, _: AuthRequired, db: DbSession) -> dict[str, object]:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return {"status": "missing", "can_cancel": False}
    return _run_status_payload(run)


@app.get("/runs/{run_id}/log/append")
async def run_log_append(
    run_id: int, _: AuthRequired, db: DbSession, offset: int = 0
) -> dict[str, object]:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return {"text": "", "offset": 0}
    return read_log_append(Path(run.log_path), offset)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is not None and not runner.cancel_job_run(run.job_run_id):
        _mark_job_run_canceled(db, run.job_run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/delete")
async def delete_run(
    run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is not None:
        _delete_step_run(db, run)
    return RedirectResponse("/runs", status_code=303)


@app.get("/runs/{run_id}/log/raw")
async def raw_run_log(run_id: int, _: AuthRequired, db: DbSession) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return RedirectResponse("/runs", status_code=303)
    return _raw_log_response(Path(run.log_path))


@app.get("/console", response_class=HTMLResponse)
async def console(request: Request, _: AuthRequired, db: DbSession) -> Response:
    recent = db.query(ConsoleRunRecord).order_by(ConsoleRunRecord.started_at.desc()).limit(20).all()
    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "recent": _console_history_rows(recent),
            "recent_commands": [record.command for record in reversed(recent)],
        },
    )


@app.get("/console/runs/{run_id}", response_class=HTMLResponse)
async def console_run_detail(
    request: Request, run_id: int, _: AuthRequired, db: DbSession
) -> Response:
    run = db.get(ConsoleRunRecord, run_id)
    if run is None:
        return RedirectResponse("/console", status_code=303)
    log_chunk = read_log_chunk(Path(run.log_path), limit=LOG_CHUNK_LINES)
    log_size = _log_size(Path(run.log_path))
    return templates.TemplateResponse(
        request,
        "console_run_detail.html",
        {
            "run": run,
            "argv": json.loads(run.argv_json),
            "log_chunk": log_chunk,
            "log_chunk_url": f"/console/runs/{run.id}/log/chunk",
            "log_append_url": "",
            "log_status_url": "",
            "log_append_offset": log_size,
            "raw_log_url": f"/console/runs/{run.id}/log/raw",
        },
    )


@app.get("/console/runs/{run_id}/log/chunk")
async def console_log_chunk(
    run_id: int, _: AuthRequired, db: DbSession, before: int | None = None
) -> dict[str, object]:
    run = db.get(ConsoleRunRecord, run_id)
    if run is None:
        return {"text": "", "next_before": None, "has_more": False}
    return _log_chunk_payload(Path(run.log_path), before)


@app.get("/console/runs/{run_id}/log/raw")
async def raw_console_log(run_id: int, _: AuthRequired, db: DbSession) -> Response:
    run = db.get(ConsoleRunRecord, run_id)
    if run is None:
        return RedirectResponse("/console", status_code=303)
    return _raw_log_response(Path(run.log_path))


@app.post("/console/runs/{run_id}/delete")
async def delete_console_run(
    run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    run = db.get(ConsoleRunRecord, run_id)
    if run is not None:
        _delete_console_run(db, run)
    return RedirectResponse("/runs", status_code=303)


@app.websocket("/console/terminal")
async def console_terminal(websocket: WebSocket) -> None:
    if websocket.session.get(SESSION_KEY) is not True:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    command_task: asyncio.Task[None] | None = None
    input_queue: asyncio.Queue[str] | None = None

    async def send_output(text: str) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json({"type": "output", "data": text})

    async def send_state(state: str) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json({"type": "state", "state": state})

    async def start_command(command: str) -> None:
        nonlocal command_task, input_queue
        if command_task is not None and not command_task.done():
            await send_output("\r\n[a command is already running]\r\n")
            return
        try:
            argv = parse_console_command(command)
        except CommandPolicyError as exc:
            await send_output(f"\r\nError: {exc}\r\nrclone> ")
            await send_state("idle")
            return

        started_at = utc_now()
        log_path = _console_log_path(started_at)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        input_queue = asyncio.Queue()
        with SessionLocal() as db:
            run_id = _start_console_run_record(db, command, argv, log_path, started_at)

        def write_log(text: str) -> None:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(text)

        async def send_and_log(text: str) -> None:
            write_log(text)
            await send_output(text)

        async def run() -> None:
            nonlocal command_task, input_queue
            await send_state("running")
            write_log(f"$ {display_argv(argv)}\r\n")
            try:
                exit_code = await bridge_pty(argv, input_queue.get, send_and_log)
                ended_at = utc_now()
                exit_line = f"\r\n[process exited with code {exit_code}]\r\n"
                write_log(exit_line)
                await send_output(exit_line)
                with SessionLocal() as db:
                    record = _finish_console_run_record(db, run_id, exit_code, ended_at)
                    if record is not None:
                        await websocket.send_json(
                            {"type": "history", "run": _console_history_row(record)}
                        )
                await send_output("rclone> ")
                await send_state("idle")
            except asyncio.CancelledError:
                ended_at = utc_now()
                write_log("\r\n[process canceled]\r\n")
                with SessionLocal() as db:
                    _cancel_console_run_record(db, run_id, ended_at)
                raise
            finally:
                command_task = None
                input_queue = None

        command_task = asyncio.create_task(run())

    await send_output("rclone> ")
    await send_state("idle")
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            message_type = payload.get("type")
            if message_type == "command":
                await start_command(str(payload.get("command", "")))
            elif message_type == "input" and input_queue is not None:
                await input_queue.put(str(payload.get("data", "")))
    except WebSocketDisconnect, RuntimeError, json.JSONDecodeError:
        if command_task is not None and not command_task.done():
            command_task.cancel()
            with suppress(asyncio.CancelledError):
                await command_task


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: AuthRequired,
    db: DbSession,
    pruned: int | None = None,
    cleared: int | None = None,
    email_saved: int | None = None,
    email_test: str | None = None,
    email_error: str | None = None,
) -> Response:
    count = _prunable_logs(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "known_logs": len(count),
            "pruned": pruned,
            "cleared": cleared,
            "email_settings": load_email_notification_settings(db),
            "email_saved": email_saved,
            "email_test": email_test,
            "email_error": email_error,
        },
    )


@app.post("/settings/email")
async def save_email_settings(
    _: AuthRequired,
    db: DbSession,
    _csrf: Annotated[None, Depends(require_csrf)],
    enabled: str | None = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    sender: str = Form(""),
    recipients: str = Form(""),
    use_starttls: str | None = Form(None),
    notify_success: str | None = Form(None),
    notify_failure: str | None = Form(None),
    notify_canceled: str | None = Form(None),
    app_base_url: str = Form(""),
    include_log_tail_lines: int = Form(200),
) -> Response:
    save_email_notification_settings(
        db,
        _email_settings_from_form(
            enabled=enabled,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            sender=sender,
            recipients=recipients,
            use_starttls=use_starttls,
            notify_success=notify_success,
            notify_failure=notify_failure,
            notify_canceled=notify_canceled,
            app_base_url=app_base_url,
            include_log_tail_lines=include_log_tail_lines,
        ),
        preserve_password=True,
    )
    return RedirectResponse("/settings?email_saved=1", status_code=303)


@app.post("/settings/email/test")
async def send_test_email(
    _: AuthRequired,
    db: DbSession,
    _csrf: Annotated[None, Depends(require_csrf)],
    enabled: str | None = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    sender: str = Form(""),
    recipients: str = Form(""),
    use_starttls: str | None = Form(None),
    notify_success: str | None = Form(None),
    notify_failure: str | None = Form(None),
    notify_canceled: str | None = Form(None),
    app_base_url: str = Form(""),
    include_log_tail_lines: int = Form(200),
) -> Response:
    notification_settings = _email_settings_from_form(
        enabled=enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        sender=sender,
        recipients=recipients,
        use_starttls=use_starttls,
        notify_success=notify_success,
        notify_failure=notify_failure,
        notify_canceled=notify_canceled,
        app_base_url=app_base_url,
        include_log_tail_lines=include_log_tail_lines,
    )
    save_email_notification_settings(db, notification_settings, preserve_password=True)
    try:
        await send_test_notification(notification_settings)
    except Exception as exc:
        return RedirectResponse(
            f"/settings?{urlencode({'email_error': str(exc)})}",
            status_code=303,
        )
    return RedirectResponse("/settings?email_test=sent", status_code=303)


@app.post("/settings/prune")
async def prune(
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    deleted = prune_logs(_prunable_logs(db), keep_days=settings.retention_days)
    return RedirectResponse(f"/settings?pruned={deleted}", status_code=303)


@app.post("/settings/clear-history")
async def clear_history(
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    cleared = _clear_history(db)
    return RedirectResponse(f"/settings?cleared={cleared}", status_code=303)


def _email_settings_from_form(
    *,
    enabled: str | None,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    sender: str,
    recipients: str,
    use_starttls: str | None,
    notify_success: str | None,
    notify_failure: str | None,
    notify_canceled: str | None,
    app_base_url: str,
    include_log_tail_lines: int,
) -> EmailNotificationSettings:
    return EmailNotificationSettings(
        enabled=enabled == "on",
        smtp_host=smtp_host.strip(),
        smtp_port=smtp_port,
        smtp_username=smtp_username.strip(),
        smtp_password=smtp_password,
        sender=sender.strip(),
        recipients=recipients.strip(),
        use_starttls=use_starttls == "on",
        notify_success=notify_success == "on",
        notify_failure=notify_failure == "on",
        notify_canceled=notify_canceled == "on",
        app_base_url=app_base_url.strip(),
        include_log_tail_lines=include_log_tail_lines,
    )


def _replace_steps(job: JobRecord, steps_text: str) -> None:
    job.steps.clear()
    for index, raw_line in enumerate(steps_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        name, separator, command = line.partition("|")
        if not separator:
            name = f"Step {index}"
            command = line
        job.steps.append(JobStepRecord(position=index, name=name.strip(), command=command.strip()))


def _steps_to_text(steps: list[JobStepRecord]) -> str:
    return "\n".join(f"{step.name}|{step.command}" for step in steps)


def _job_rows(records: list[JobRecord], db) -> list[dict[str, object]]:
    latest_runs = _latest_job_runs_by_job_id(records, db)
    return [
        {
            "record": record,
            "schedule_summary": cron_summary(record.cron),
            "last_run": latest_runs.get(record.id),
            "next_run": next_run_time(record.cron) if record.enabled else None,
        }
        for record in records
    ]


def _latest_job_runs_by_job_id(records: list[JobRecord], db) -> dict[int, JobRunRecord]:
    job_ids = [record.id for record in records]
    if not job_ids:
        return {}
    runs = (
        db.query(JobRunRecord)
        .filter(JobRunRecord.job_id.in_(job_ids))
        .order_by(JobRunRecord.job_id, JobRunRecord.started_at.desc())
        .all()
    )
    latest_runs = {}
    for run in runs:
        latest_runs.setdefault(run.job_id, run)
    return latest_runs


def _command_previews(job: JobRecord) -> list[dict[str, str]]:
    env = parse_env_lines(env_to_lines(job.env_json))
    previews = []
    for step in job.steps:
        try:
            argv = build_rclone_argv(step.command, job.common_args or "", env)
            command = display_argv(argv)
        except CommandPolicyError as exc:
            command = f"Invalid command: {exc}"
            tokens = [command]
        else:
            tokens = [display_argv([part]) for part in argv]
        previews.append({"id": step.id, "name": step.name, "command": command, "tokens": tokens})
    return previews


def _delete_job_configuration(db, job: JobRecord) -> None:
    step_ids = [step.id for step in job.steps]
    if step_ids:
        (
            db.query(JobStepRunRecord)
            .filter(JobStepRunRecord.step_id.in_(step_ids))
            .update({JobStepRunRecord.step_id: None}, synchronize_session="fetch")
        )
    (
        db.query(JobRunRecord)
        .filter_by(job_id=job.id)
        .update({JobRunRecord.job_id: None}, synchronize_session="fetch")
    )
    db.delete(job)


def _prunable_logs(db) -> list[tuple[Path, datetime]]:
    step_logs = [
        (Path(item.log_path), item.started_at) for item in db.query(JobStepRunRecord).all()
    ]
    console_logs = [
        (Path(item.log_path), item.started_at) for item in db.query(ConsoleRunRecord).all()
    ]
    return step_logs + console_logs


def _clear_history(db) -> int:
    logs = _prunable_logs(db)
    deleted_records = db.query(JobRunRecord).count() + db.query(ConsoleRunRecord).count()
    for path, _started_at in logs:
        if path.exists():
            path.unlink()
    db.query(ConsoleRunRecord).delete(synchronize_session=False)
    db.query(JobStepRunRecord).delete(synchronize_session=False)
    db.query(JobRunRecord).delete(synchronize_session=False)
    db.commit()
    return deleted_records


def _mark_job_run_canceled(db, job_run_id: int) -> None:
    now = utc_now()
    job_run = db.get(JobRunRecord, job_run_id)
    if job_run is None or job_run.status != "running":
        return
    job_run.status = "canceled"
    job_run.ended_at = now
    for step_run in job_run.step_runs:
        if step_run.status == "running":
            step_run.status = "canceled"
            step_run.exit_code = None
            step_run.ended_at = now
    db.commit()


def _delete_job_run(db, job_run: JobRunRecord) -> None:
    for step_run in list(job_run.step_runs):
        _delete_log_file(Path(step_run.log_path))
    db.delete(job_run)
    db.commit()


def _delete_step_run(db, step_run: JobStepRunRecord) -> None:
    _delete_log_file(Path(step_run.log_path))
    db.delete(step_run)
    db.commit()


def _delete_console_run(db, run: ConsoleRunRecord) -> None:
    _delete_log_file(Path(run.log_path))
    db.delete(run)
    db.commit()


def _delete_log_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def _console_history_rows(records: list[ConsoleRunRecord]) -> list[dict[str, object]]:
    return [_console_history_row(record) for record in records]


def _paginated_history(query, page: int, page_param: str, other_pages: dict[str, int]):
    current_page = max(1, page)
    total_items = query.count()
    total_pages = max(1, ceil(total_items / HISTORY_PAGE_SIZE))
    offset = (current_page - 1) * HISTORY_PAGE_SIZE
    items = query.offset(offset).limit(HISTORY_PAGE_SIZE).all()
    has_next = current_page < total_pages
    pagination = {
        "page": current_page,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": has_next,
        "previous_url": _history_page_url(
            page_param, current_page - 1, other_pages, _history_section_id(page_param)
        )
        if current_page > 1
        else None,
        "next_url": _history_page_url(
            page_param, current_page + 1, other_pages, _history_section_id(page_param)
        )
        if has_next
        else None,
        "target": _history_section_id(page_param),
    }
    return items, pagination


def _history_page_url(
    page_param: str, page: int, other_pages: dict[str, int], section_id: str | None = None
) -> str:
    params = {page_param: max(1, page)}
    for name, value in other_pages.items():
        params[name] = max(1, value)
    url = "/runs?" + urlencode(params)
    if section_id is not None:
        url += f"#{section_id}"
    return url


def _history_section_id(page_param: str) -> str:
    return {
        "job_page": "job-runs-section",
        "step_page": "step-runs-section",
        "console_page": "console-runs-section",
    }[page_param]


def _start_console_run_record(
    db, command: str, argv: list[str], log_path: Path, started_at: datetime
) -> int:
    record = ConsoleRunRecord(
        status="running",
        command=command,
        argv_json=json.dumps(argv),
        exit_code=None,
        log_path=str(log_path),
        started_at=started_at,
        ended_at=None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def _finish_console_run_record(
    db, run_id: int, exit_code: int, ended_at: datetime
) -> ConsoleRunRecord | None:
    record = db.get(ConsoleRunRecord, run_id)
    if record is None:
        return None
    record.status = "success" if exit_code == 0 else "failed"
    record.exit_code = exit_code
    record.ended_at = ended_at
    db.commit()
    db.refresh(record)
    return record


def _cancel_console_run_record(db, run_id: int, ended_at: datetime) -> ConsoleRunRecord | None:
    record = db.get(ConsoleRunRecord, run_id)
    if record is None:
        return None
    record.status = "canceled"
    record.exit_code = None
    record.ended_at = ended_at
    db.commit()
    db.refresh(record)
    return record


def _console_history_row(record: ConsoleRunRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "command": record.command,
        "started_at": _format_local_time(record.started_at),
        "exit_code": record.exit_code,
    }


def _ongoing_job_run_rows(records: list[JobRunRecord]) -> list[dict[str, object]]:
    return [
        {
            "run": record,
            "current_step": active_step.step_name if active_step is not None else "Starting",
        }
        for record in records
        for active_step in [_active_step_run(record)]
    ]


def _job_run_status_payload(job_run: JobRunRecord, db) -> dict[str, object]:
    finished_at = _format_local_time(job_run.ended_at) if job_run.ended_at is not None else None
    end = job_run.ended_at or utc_now()
    elapsed_seconds = _duration_seconds(job_run.started_at, end)
    active_step_run = _active_step_run(job_run)
    run_stats = _job_run_stats(job_run)
    return {
        "status": job_run.status,
        "started_at": _format_local_time(job_run.started_at),
        "finished_at": finished_at,
        "elapsed": _format_seconds(elapsed_seconds),
        "elapsed_seconds": elapsed_seconds,
        "can_cancel": job_run.status == "running",
        "active_step_run_id": active_step_run.id if active_step_run is not None else None,
        "stats": {
            "transferred_data": run_stats.transferred_data_label,
            "transferred_files": run_stats.transferred_files_label,
            "deleted_files": run_stats.deleted_files_label,
        },
        "steps": [_job_run_step_payload(row) for row in _job_run_step_rows(job_run, db)],
    }


def _job_run_step_payload(row: dict[str, object]) -> dict[str, object]:
    step_run = row["run"]
    status = str(row["status"])
    return {
        "key": row["key"],
        "name": row["name"],
        "status": status,
        "run_id": step_run.id if step_run is not None else None,
        "exit_label": _exit_label(status, step_run.exit_code if step_run is not None else None),
        "stats_label": row["stats"].label,
        "stats_available": row["stats"].available,
        "started_at": _format_local_time(step_run.started_at) if step_run is not None else None,
        "duration": _step_duration_label(step_run),
    }


def _job_run_step_rows(job_run: JobRunRecord, db) -> list[dict[str, object]]:
    step_runs = sorted(job_run.step_runs, key=lambda item: item.started_at)
    step_run_by_step_id = {
        step_run.step_id: step_run for step_run in step_runs if step_run.step_id is not None
    }
    if job_run.job_id is not None and "step" not in job_run.trigger:
        job = db.get(JobRecord, job_run.job_id)
        if job is not None:
            rows = []
            for step in job.steps:
                step_run = step_run_by_step_id.get(step.id)
                status = (
                    step_run.status if step_run is not None else _unstarted_step_status(job_run)
                )
                rows.append(
                    {
                        "key": f"step-{step.id}",
                        "name": step.name,
                        "status": status,
                        "run": step_run,
                        "stats": _step_stats_display_for_run(step_run),
                        "timing": _step_timing_label(step_run),
                    }
                )
            return rows

    return [
        {
            "key": f"step-{step_run.step_id}"
            if step_run.step_id is not None
            else f"run-{step_run.id}",
            "name": step_run.step_name,
            "status": step_run.status,
            "run": step_run,
            "stats": _step_stats_display_for_run(step_run),
            "timing": _step_timing_label(step_run),
        }
        for step_run in step_runs
    ]


def _active_step_run(job_run: JobRunRecord) -> JobStepRunRecord | None:
    step_runs = sorted(job_run.step_runs, key=lambda item: item.started_at)
    running = [step_run for step_run in step_runs if step_run.status == "running"]
    if running:
        return running[-1]
    if step_runs:
        return step_runs[-1]
    return None


def _unstarted_step_status(job_run: JobRunRecord) -> str:
    if job_run.status == "canceled":
        return "canceled"
    return "pending"


def _run_status_payload(run: JobStepRunRecord) -> dict[str, object]:
    finished_at = _format_local_time(run.ended_at) if run.ended_at is not None else None
    end = run.ended_at or utc_now()
    elapsed_seconds = _duration_seconds(run.started_at, end)
    stats = _step_stats_display_for_run(run)
    return {
        "status": run.status,
        "job_status": run.job_run.status,
        "exit_code": run.exit_code,
        "exit_label": _exit_label(run.status, run.exit_code),
        "started_at": _format_local_time(run.started_at),
        "finished_at": finished_at,
        "elapsed": _format_seconds(elapsed_seconds),
        "elapsed_seconds": elapsed_seconds,
        "can_cancel": run.status == "running",
        "stats_label": stats.label,
        "stats_available": stats.available,
    }


def _job_run_stats(job_run: JobRunRecord) -> RunStatsDisplay:
    values: list[RcloneTransferStats | None] = []
    for step_run in job_run.step_runs:
        if step_run.status == "running" or step_run.ended_at is None:
            continue
        stats = _step_transfer_stats(step_run)
        values.append(stats)
    return run_stats_display(values)


def _step_stats_display_for_run(step_run: JobStepRunRecord | None):
    if step_run is None or step_run.status == "running" or step_run.ended_at is None:
        return pending_stats_display("Pending")
    return step_stats_display(_step_transfer_stats(step_run))


def _step_timing_label(step_run: JobStepRunRecord | None) -> dict[str, str]:
    if step_run is None:
        return {"started": "Pending", "duration": "Pending"}
    return {
        "started": _format_local_time(step_run.started_at),
        "duration": _step_duration_label(step_run),
    }


def _step_duration_label(step_run: JobStepRunRecord | None) -> str:
    if step_run is None:
        return "Pending"
    ended_at = step_run.ended_at or utc_now()
    return _format_duration(step_run.started_at, ended_at)


def _step_transfer_stats(step_run: JobStepRunRecord) -> RcloneTransferStats | None:
    stored = stats_from_json(step_run.transfer_stats_json)
    if stored is not None:
        return stored
    if step_run.ended_at is None:
        return None
    return stats_from_log(Path(step_run.log_path))


def _exit_label(status: str, exit_code: int | None) -> str:
    if exit_code is not None:
        return str(exit_code)
    if status == "canceled":
        return "Canceled"
    if status == "skipped":
        return "Skipped"
    return "Pending"


def _console_log_path(started_at: datetime) -> Path:
    return settings.log_dir / "console" / f"{started_at.strftime('%Y%m%dT%H%M%S%fZ')}.log"


def _format_local_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local_time = value.astimezone(ZoneInfo(settings.timezone))
    return local_time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_duration(started_at: datetime, ended_at: datetime) -> str:
    return _format_seconds(_duration_seconds(started_at, ended_at))


def _duration_seconds(started_at: datetime, ended_at: datetime) -> float:
    if started_at.tzinfo is None and ended_at.tzinfo is not None:
        ended_at = ended_at.replace(tzinfo=None)
    elif started_at.tzinfo is not None and ended_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    return max(0.0, (ended_at - started_at).total_seconds())


def _format_seconds(total_seconds: float) -> str:
    if total_seconds < 60:
        return f"{total_seconds:.1f}s"

    seconds = int(total_seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _log_chunk_payload(path: Path, before: int | None) -> dict[str, object]:
    chunk = read_log_chunk(path, before=before, limit=LOG_CHUNK_LINES)
    return {
        "text": chunk.text,
        "next_before": chunk.next_before,
        "has_more": chunk.has_more,
    }


def _log_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size


def _raw_log_response(path: Path) -> Response:
    if not path.exists():
        return Response("Log file has been pruned or is not available.", status_code=404)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)

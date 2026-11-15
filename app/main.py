from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect

from app.auth import (
    SESSION_KEY,
    AuthRequired,
    LoginThrottle,
    csrf_field,
    require_csrf,
    verify_password,
)
from app.config import runtime_warnings, settings, startup_permission_error
from app.core.commands import (
    CommandPolicyError,
    build_step_argv,
    parse_console_command,
    run_variables,
)
from app.core.estimates import (
    SAMPLE_RUNS,
    bwlimit_bytes_per_second,
    choose_speed,
    estimate_seconds,
    format_estimate,
    is_upload_sample,
    load_expected_speed,
    observed_speed,
    save_expected_speed,
)
from app.core.live_control import RateError, RcError, configured_rate, rate_label
from app.core.logs import read_log_append, read_log_chunk
from app.core.models import utc_now
from app.core.notifications import (
    EmailNotificationSettings,
    load_email_notification_settings,
    save_email_notification_settings,
    send_test_notification,
)
from app.core.prune import PruneCommandError, dry_run_requested, is_prune_command
from app.core.prune import from_argv as prune_from_argv
from app.core.pty_console import bridge_pty, display_argv
from app.core.rclone_stats import (
    LiveProgress,
    RcloneTransferStats,
    RunStatsDisplay,
    format_byte_count,
    live_progress_from_log,
    pending_stats_display,
    run_stats_display,
    step_stats_display,
    step_transfer_stats,
)
from app.core.retention import prune_logs
from app.core.schedule import cron_summary, normalize_cron
from app.core.sources import (
    SizeCommand,
    ensure_flags,
    flags_available,
    size_command,
    split_command,
)
from app.db import (
    ConsoleRunRecord,
    DbSession,
    JobRecord,
    JobRunRecord,
    JobStepRecord,
    JobStepRunRecord,
    SessionLocal,
    SourceMeasurementRecord,
    env_to_lines,
    init_db,
    parse_env_lines,
    record_to_job,
)
from app.runner_service import measurer, runner
from app.scheduler import cron_error, next_run_time, scheduler, sync_schedules


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
templates.env.globals["local_time"] = lambda value: _local_time_html(value)
templates.env.globals["format_duration"] = lambda start, end: _format_duration(start, end)
templates.env.globals["format_seconds"] = lambda seconds: _format_seconds(seconds)
templates.env.globals["utc_now"] = utc_now
templates.env.globals["format_bytes"] = lambda value: format_byte_count(value)
templates.env.globals["run_exit_label"] = lambda run: _exit_label(run.status, run.exit_code)


logger = logging.getLogger(__name__)

LOG_CHUNK_LINES = 200
login_throttle = LoginThrottle()
HISTORY_PAGE_SIZE = 25
# How far back estimates look for speed samples and dry runs.
HISTORY_SCAN_LIMIT = 2000
BANDWIDTH_PRESETS = [
    ("off", "Unlimited"),
    *((rate, rate_label(rate)) for rate in ("100M", "50M", "20M", "10M", "5M", "2M", "1M", "512K")),
]
CONTROL_ERRORS = {
    "rate": "That is not a bandwidth limit. Use a number with K, M or G, such as 750K or 8M.",
    "ended": "The run has finished, so there is nothing to adjust.",
    "unreachable": (
        "The running rclone did not respond, so the change applies from the next step. "
        "Details are in the app log."
    ),
}
HISTORY_VIEWS = ("jobs", "steps", "console")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    permission_error = startup_permission_error(settings)
    if permission_error is not None:
        raise RuntimeError(permission_error)
    init_db()
    runner.recover_interrupted_runs()
    measurer.recover_interrupted()
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
    client = request.client.host if request.client else "unknown"
    retry_after = login_throttle.retry_after(client)
    if retry_after is not None:
        minutes = ceil(retry_after / 60)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": f"Too many failed logins. Try again in {minutes} min."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    if username == settings.admin_user and verify_password(password):
        login_throttle.reset(client)
        request.session[SESSION_KEY] = True
        return RedirectResponse("/jobs", status_code=303)
    login_throttle.record_failure(client)
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
    running_job_runs = (
        db.query(JobRunRecord).filter_by(status="running").order_by(JobRunRecord.started_at).all()
    )
    running_console_runs = (
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
            "running_now": _running_now_rows(running_job_runs, running_console_runs),
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


@dataclass(frozen=True, slots=True)
class JobFormValues:
    """Submitted job editor values, re-rendered when the form is rejected."""

    name: str
    cron: str
    common_args: str
    enabled: bool


@app.post("/jobs")
async def create_job(
    request: Request,
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
    form_error = cron_error(cron)
    if form_error is not None:
        return templates.TemplateResponse(
            request,
            "job_form.html",
            {
                "job": None,
                "form_values": JobFormValues(name, cron, common_args, enabled == "on"),
                "form_error": form_error,
                "schedule_summary": cron_summary(cron),
                "env_lines": env_lines,
                "steps_text": steps_text,
                "command_previews": [],
            },
            status_code=400,
        )
    job = JobRecord(
        name=name,
        cron=normalize_cron(cron),
        enabled=enabled == "on",
        common_args=common_args,
        env_json=json.dumps(parse_env_lines(env_lines)),
    )
    db.add(job)
    db.flush()
    _replace_steps(db, job, steps_text)
    db.commit()
    sync_schedules(db)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request,
    job_id: int,
    _: AuthRequired,
    db: DbSession,
    delete_blocked: bool = False,
    duplicated: bool = False,
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    await ensure_flags()
    context = _job_detail_context(job, db) | {
        "delete_blocked": delete_blocked,
        "duplicated": duplicated,
    }
    return templates.TemplateResponse(request, "job_detail.html", context)


def _job_detail_context(job: JobRecord, db) -> dict[str, object]:
    job_runs = (
        db.query(JobRunRecord)
        .filter_by(job_id=job.id)
        .order_by(JobRunRecord.started_at.desc())
        .limit(20)
        .all()
    )
    runs = (
        db.query(JobStepRunRecord)
        .join(JobStepRunRecord.job_run)
        .filter_by(job_id=job.id)
        .order_by(JobStepRunRecord.started_at.desc())
        .limit(20)
        .all()
    )
    return _sources_context(job, db) | {
        "job": job,
        "schedule_summary": cron_summary(job.cron),
        "env_lines": env_to_lines(job.env_json),
        "steps_text": _steps_to_text(job.steps),
        "command_previews": _command_previews(job),
        "run_warning": _run_warning(job),
        "job_run_rows": [_history_row("jobs", job_run) for job_run in job_runs],
        "runs": runs,
        "delete_blocked": False,
    }


@app.post("/jobs/{job_id}/measure")
async def measure_job_sources(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    await ensure_flags()
    commands = [command for _step, command in _step_size_commands(job) if command is not None]
    measurer.start(commands, parse_env_lines(env_to_lines(job.env_json)))
    return RedirectResponse(f"/jobs/{job_id}#step-sources", status_code=303)


def _step_size_commands(job: JobRecord) -> list[tuple[JobStepRecord, SizeCommand | None]]:
    """Each step with the rclone size command for its source, or None without one."""
    return [(step, command) for step, _argv, command in _step_commands(job)]


def _step_commands(job: JobRecord):
    """Each step with its rclone command (None if invalid) and the size command for its
    source (None without one)."""
    job_env = parse_env_lines(env_to_lines(job.env_json))
    env = run_variables(utc_now(), settings.timezone) | job_env
    rows = []
    for step in sorted(job.steps, key=lambda item: item.position):
        try:
            argv = build_step_argv(step.command, job.common_args or "", env)
        except CommandPolicyError:
            rows.append((step, None, None))
            continue
        rows.append((step, argv, size_command(argv, job_env)))
    return rows


def _sources_context(job: JobRecord, db) -> dict[str, object]:
    steps = _step_commands(job)
    keys = [command.key for _step, _argv, command in steps if command is not None]
    records = {
        record.key: record
        for record in db.query(SourceMeasurementRecord)
        .filter(SourceMeasurementRecord.key.in_(keys))
        .all()
    }
    observed = observed_speed(_upload_samples(job, db))
    expected = load_expected_speed(db)
    base_speed = choose_speed(observed, expected, None)
    dry_runs = _latest_dry_run_stats(job, db)
    rows = []
    total_bytes = total_files = 0
    total_seconds = 0.0
    all_measured = bool(keys)
    all_estimated = True
    for step, argv, command in steps:
        measurement = records.get(command.key) if command is not None else None
        measured = measurement is not None and measurement.bytes is not None
        # A step's own --bwlimit caps only that step.
        speed = choose_speed(
            observed, expected, bwlimit_bytes_per_second(configured_rate(argv or []))
        )
        full_seconds = None
        if command is not None:
            if measured:
                total_bytes += measurement.bytes
                total_files += measurement.files or 0
                if speed is not None:
                    full_seconds = estimate_seconds(measurement.bytes, speed)
                    total_seconds += full_seconds
                else:
                    all_estimated = False
            else:
                all_measured = False
        dry_run = dry_runs.get(step.id)
        rows.append(
            {
                "step": step,
                "source": command.source if command is not None else None,
                "measurement": measurement,
                "full_estimate": format_estimate(full_seconds)
                if full_seconds is not None
                else None,
                "dry_run": dry_run,
                "next_estimate": format_estimate(estimate_seconds(dry_run["bytes"], speed))
                if dry_run is not None and speed is not None
                else None,
                "speed_note": speed.explanation
                if speed is not None and speed != base_speed
                else None,
            }
        )
    totals = None
    if all_measured and len(keys) > 1:
        totals = {
            "bytes": total_bytes,
            "files": total_files,
            "estimate": format_estimate(total_seconds) if all_estimated else None,
        }
    return {
        "source_rows": rows,
        "source_speed": base_speed,
        "source_totals": totals,
        "flags_unavailable": not flags_available(),
    }


def _recent_successful_step_runs(job: JobRecord, db):
    """The job's successful step runs, newest first, read in batches.

    Dry runs can only be told apart by reading each command, so callers filter as they go
    and stop when they have enough; HISTORY_SCAN_LIMIT bounds the search.
    """
    return (
        db.query(JobStepRunRecord)
        .join(JobStepRunRecord.job_run)
        .filter(
            JobRunRecord.job_id == job.id,
            JobStepRunRecord.status == "success",
            JobStepRunRecord.ended_at.is_not(None),
        )
        .order_by(JobStepRunRecord.started_at.desc())
        .limit(HISTORY_SCAN_LIMIT)
        .yield_per(100)
    )


def _upload_samples(job: JobRecord, db) -> list[tuple[int, float]]:
    """(bytes, seconds) of the job's recent real step runs that moved data, newest first."""
    samples: list[tuple[int, float]] = []
    sizeable = 0
    for step_run in _recent_successful_step_runs(job, db):
        if _is_dry_run(step_run):
            continue
        stats = _step_transfer_stats(step_run)
        if stats is None:
            continue
        seconds = _duration_seconds(step_run.started_at, step_run.ended_at)
        samples.append((stats.transferred_bytes, seconds))
        sizeable += is_upload_sample(stats.transferred_bytes, seconds)
        if sizeable >= SAMPLE_RUNS:
            break
    return samples


def _is_dry_run(step_run: JobStepRunRecord) -> bool:
    """A dry run by the Dry run button, or by --dry-run or -n in the command itself."""
    if "dry-run" in step_run.job_run.trigger:
        return True
    try:
        argv = json.loads(step_run.argv_json)
    except ValueError:
        return False
    if is_prune_command(argv):
        try:
            return prune_from_argv(argv).dry_run
        except PruneCommandError:
            return False
    if len(argv) < 2:
        return False
    flags, _paths = split_command(argv)
    return dry_run_requested(flag[0] for flag in flags)


def _latest_dry_run_stats(job: JobRecord, db) -> dict[int, dict[str, object]]:
    """What each step's latest successful dry run said it would transfer."""
    step_ids = {step.id for step in job.steps}
    latest: dict[int, dict[str, object]] = {}
    for step_run in _recent_successful_step_runs(job, db):
        if step_run.step_id not in step_ids or step_run.step_id in latest:
            continue
        if not _is_dry_run(step_run):
            continue
        stats = _step_transfer_stats(step_run)
        latest[step_run.step_id] = {
            "bytes": stats.transferred_bytes if stats is not None else 0,
            "files": stats.transferred_files if stats is not None else 0,
            "at": step_run.started_at,
        }
        if len(latest) == len(step_ids):
            break
    return latest


@app.post("/jobs/{job_id}")
async def update_job(
    request: Request,
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
    form_error = cron_error(cron)
    if form_error is not None:
        await ensure_flags()
        context = _job_detail_context(job, db) | {
            "form_values": JobFormValues(name, cron, common_args, enabled == "on"),
            "form_error": form_error,
            "schedule_summary": cron_summary(cron),
            "env_lines": env_lines,
            "steps_text": steps_text,
        }
        return templates.TemplateResponse(request, "job_detail.html", context, status_code=400)
    job.name = name
    job.cron = normalize_cron(cron)
    job.common_args = common_args
    job.env_json = json.dumps(parse_env_lines(env_lines))
    job.enabled = enabled == "on"
    job.updated_at = utc_now()
    _replace_steps(db, job, steps_text)
    db.commit()
    sync_schedules(db)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/duplicate")
async def duplicate_job(
    job_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    # The copy starts disabled so it cannot run on the original's schedule, usually against
    # the same destination, before it has been edited.
    copy = JobRecord(
        name=f"{job.name} (copy)",
        cron=job.cron,
        enabled=False,
        common_args=job.common_args,
        env_json=job.env_json,
    )
    for step in job.steps:
        copy.steps.append(
            JobStepRecord(position=step.position, name=step.name, command=step.command)
        )
    db.add(copy)
    db.commit()
    return RedirectResponse(f"/jobs/{copy.id}?duplicated=1", status_code=303)


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
    view: str = "jobs",
    page: int = 1,
    delete_blocked: bool = False,
) -> Response:
    if view not in HISTORY_VIEWS:
        view = "jobs"
    queries = {
        "jobs": db.query(JobRunRecord).order_by(JobRunRecord.started_at.desc()),
        "steps": db.query(JobStepRunRecord).order_by(JobStepRunRecord.started_at.desc()),
        "console": db.query(ConsoleRunRecord).order_by(ConsoleRunRecord.started_at.desc()),
    }
    items, pagination = _paginated_history(queries[view], view=view, page=page)
    rows = [_history_row(view, item) for item in items]
    tabs = [
        {
            "view": name,
            "label": label,
            "count": query.count() if name != view else pagination["total_items"],
            "url": _history_url(name),
            "current": name == view,
        }
        for name, (label, query) in (
            ("jobs", ("Job runs", queries["jobs"])),
            ("steps", ("Step runs", queries["steps"])),
            ("console", ("Console", queries["console"])),
        )
    ]
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "view": view,
            "tabs": tabs,
            "rows": rows,
            "pagination": pagination,
            "delete_blocked": delete_blocked,
        },
    )


@app.get("/job-runs/{job_run_id}", response_class=HTMLResponse)
async def job_run_detail(
    request: Request,
    job_run_id: int,
    _: AuthRequired,
    db: DbSession,
    control_error: str | None = None,
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
        "progress": _progress_text(active_step_run),
        "job_run_status_url": f"/job-runs/{job_run.id}/status",
        "job_run_cancel_url": f"/job-runs/{job_run.id}/cancel",
        "control": runner.control_state(job_run.id) if job_run.status == "running" else None,
        "control_error": CONTROL_ERRORS.get(control_error or ""),
        "bandwidth_presets": BANDWIDTH_PRESETS,
    }
    if active_step_run is not None:
        log_chunk = read_log_chunk(Path(active_step_run.log_path), limit=LOG_CHUNK_LINES)
        context |= {
            "run": active_step_run,
            "argv": json.loads(active_step_run.argv_json),
            "log_chunk": log_chunk,
            "log_chunk_url": f"/runs/{active_step_run.id}/log/chunk",
            "log_append_url": f"/runs/{active_step_run.id}/log/append",
            "log_status_url": f"/runs/{active_step_run.id}/status",
            # From the same read as the text, so nothing written in between is skipped.
            "log_append_offset": log_chunk.end,
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


@app.post("/job-runs/{job_run_id}/bandwidth")
async def set_job_run_bandwidth(
    job_run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    rate: Annotated[str, Form()] = "job",
    custom: Annotated[str, Form()] = "",
) -> Response:
    choice = custom.strip() or rate
    return await _control_job_run(
        job_run_id, runner.set_bandwidth(job_run_id, None if choice == "job" else choice)
    )


@app.post("/job-runs/{job_run_id}/pause")
async def pause_job_run(
    job_run_id: int, _: AuthRequired, _csrf: Annotated[None, Depends(require_csrf)]
) -> Response:
    return await _control_job_run(job_run_id, runner.pause(job_run_id))


@app.post("/job-runs/{job_run_id}/resume")
async def resume_job_run(
    job_run_id: int, _: AuthRequired, _csrf: Annotated[None, Depends(require_csrf)]
) -> Response:
    return await _control_job_run(job_run_id, runner.resume(job_run_id))


async def _control_job_run(job_run_id: int, action) -> Response:
    error = None
    try:
        await action
    except RateError:
        error = "rate"
    except KeyError:
        error = "ended"
    except RcError:
        logger.warning("Live control of job run %s failed", job_run_id, exc_info=True)
        error = "unreachable"
    url = f"/job-runs/{job_run_id}"
    if error is not None:
        url += f"?control_error={error}"
    return RedirectResponse(url, status_code=303)


@app.post("/job-runs/{job_run_id}/delete")
async def delete_job_run(
    job_run_id: int,
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
    page: Annotated[int, Form()] = 1,
) -> Response:
    job_run = db.get(JobRunRecord, job_run_id)
    if job_run is not None and job_run.status == "running":
        return RedirectResponse(_history_url("jobs", page, delete_blocked=True), status_code=303)
    if job_run is not None:
        _delete_job_run(db, job_run)
    return RedirectResponse(_history_url("jobs", page), status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int, _: AuthRequired, db: DbSession) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return RedirectResponse("/runs", status_code=303)
    log_chunk = read_log_chunk(Path(run.log_path), limit=LOG_CHUNK_LINES)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "argv": json.loads(run.argv_json),
            "step_stats": _step_stats_display_for_run(run),
            "progress": _progress_text(run),
            "job_run_step_count": len(run.job_run.step_runs),
            "log_chunk": log_chunk,
            "log_chunk_url": f"/runs/{run.id}/log/chunk",
            "log_append_url": f"/runs/{run.id}/log/append",
            "log_status_url": f"/runs/{run.id}/status",
            "log_append_offset": log_chunk.end,
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
    page: Annotated[int, Form()] = 1,
) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is not None and "running" in {run.status, run.job_run.status}:
        return RedirectResponse(_history_url("steps", page, delete_blocked=True), status_code=303)
    if run is not None:
        _delete_step_run(db, run)
    return RedirectResponse(_history_url("steps", page), status_code=303)


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
            "log_append_offset": log_chunk.end,
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
    page: Annotated[int, Form()] = 1,
) -> Response:
    run = db.get(ConsoleRunRecord, run_id)
    if run is not None and run.status == "running":
        return RedirectResponse(_history_url("console", page, delete_blocked=True), status_code=303)
    if run is not None:
        _delete_console_run(db, run)
    return RedirectResponse(_history_url("console", page), status_code=303)


@app.websocket("/console/terminal")
async def console_terminal(websocket: WebSocket) -> None:
    if not _is_same_origin(websocket) or websocket.session.get(SESSION_KEY) is not True:
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
            try:
                write_log(f"$ {display_argv(argv)}\r\n")
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
                with suppress(OSError):
                    write_log("\r\n[process canceled]\r\n")
                with SessionLocal() as db:
                    _cancel_console_run_record(db, run_id, ended_at)
                raise
            except Exception as exc:
                # For example the console log hit a full disk; bridge_pty has stopped the command.
                logger.exception("Console command %s failed", run_id)
                ended_at = utc_now()
                with suppress(OSError):
                    write_log(f"\r\n[rclone-runner] Console command failed: {exc!r}\r\n")
                with SessionLocal() as db:
                    _fail_console_run_record(db, run_id, ended_at)
                await send_output(f"\r\nError: {exc}\r\nrclone> ")
                await send_state("idle")
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


def _is_same_origin(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket handshakes, which browsers send with the session cookie.

    Browsers always send Origin on WebSocket handshakes and page scripts cannot forge it or
    the forwarding headers, so an Origin matching Host (or the proxy's X-Forwarded-Host)
    means the page was served by this app.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    origin_host = urlsplit(origin).netloc.lower()
    allowed_hosts = {
        value.strip().lower()
        for header in ("host", "x-forwarded-host")
        for value in websocket.headers.get(header, "").split(",")
        if value.strip()
    }
    return origin_host in allowed_hosts


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: AuthRequired,
    db: DbSession,
    pruned: int | None = None,
    cleared: int | None = None,
    kept: int | None = None,
    email_saved: int | None = None,
    email_test: str | None = None,
    email_error: str | None = None,
    speed_saved: int | None = None,
    speed_error: int | None = None,
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
            "kept": kept,
            "email_settings": load_email_notification_settings(db),
            "email_saved": email_saved,
            "email_test": email_test,
            "email_error": email_error,
            "expected_speed": load_expected_speed(db),
            "speed_saved": speed_saved,
            "speed_error": speed_error,
        },
    )


@app.post("/settings/estimates")
async def save_estimate_settings(
    _: AuthRequired,
    _csrf: Annotated[None, Depends(require_csrf)],
    db: DbSession,
    expected_speed: Annotated[str, Form()] = "",
) -> Response:
    value = expected_speed.strip().replace(",", ".")
    if not value:
        save_expected_speed(db, None)
        return RedirectResponse("/settings?speed_saved=1#estimates", status_code=303)
    try:
        speed = float(value)
    except ValueError:
        speed = -1.0
    if not (0 < speed < 1_000_000):
        return RedirectResponse("/settings?speed_error=1#estimates", status_code=303)
    save_expected_speed(db, speed)
    return RedirectResponse("/settings?speed_saved=1#estimates", status_code=303)


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
    use_ssl: str | None = Form(None),
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
            use_ssl=use_ssl,
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
    use_ssl: str | None = Form(None),
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
        use_ssl=use_ssl,
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
    cleared, kept = _clear_history(db)
    query = {"cleared": cleared} | ({"kept": kept} if kept else {})
    return RedirectResponse(f"/settings?{urlencode(query)}", status_code=303)


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
    use_ssl: str | None,
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
        use_ssl=use_ssl == "on",
        notify_success=notify_success == "on",
        notify_failure=notify_failure == "on",
        notify_canceled=notify_canceled == "on",
        app_base_url=app_base_url.strip(),
        include_log_tail_lines=include_log_tail_lines,
    )


def _replace_steps(db, job: JobRecord, steps_text: str) -> None:
    """Apply the edited step list, keeping the IDs of steps whose names are unchanged.

    Step runs refer to steps by ID, so reusing records keeps a job's run history attached to
    its steps when commands are edited or steps are reordered.
    """
    unused_by_name: dict[str, list[JobStepRecord]] = {}
    for step in job.steps:
        unused_by_name.setdefault(step.name, []).append(step)

    steps: list[JobStepRecord] = []
    for index, raw_line in enumerate(steps_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        name, separator, command = line.partition("|")
        if not separator:
            name = f"Step {index}"
            command = line
        name = name.strip()
        candidates = unused_by_name.get(name)
        if candidates:
            step = candidates.pop(0)
            step.position = index
            step.command = command.strip()
        else:
            step = JobStepRecord(position=index, name=name, command=command.strip())
        steps.append(step)

    removed_ids = [
        step.id for unused in unused_by_name.values() for step in unused if step.id is not None
    ]
    if removed_ids:
        # Keep history rows, but stop them pointing at IDs SQLite may hand out again.
        (
            db.query(JobStepRunRecord)
            .filter(JobStepRunRecord.step_id.in_(removed_ids))
            .update({JobStepRunRecord.step_id: None}, synchronize_session="fetch")
        )
    job.steps = steps


def _steps_to_text(steps: list[JobStepRecord]) -> str:
    return "\n".join(f"{step.name}|{step.command}" for step in steps)


def _job_rows(records: list[JobRecord], db) -> list[dict[str, object]]:
    latest_runs = _latest_job_runs_by_job_id(records, db)
    return [
        {
            "record": record,
            "run_warning": _run_warning(record),
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


def _command_previews(job: JobRecord) -> list[dict[str, object]]:
    # Previews show what a run started now would use.
    env = run_variables(utc_now(), settings.timezone) | parse_env_lines(env_to_lines(job.env_json))
    previews = []
    for step in job.steps:
        deletes = None
        try:
            argv = build_step_argv(step.command, job.common_args or "", env)
            command = display_argv(argv)
        except CommandPolicyError as exc:
            command = f"Invalid command: {exc}"
            tokens = [command]
        else:
            tokens = [display_argv([part]) for part in argv]
            deletes = _prune_description(argv)
        previews.append(
            {
                "id": step.id,
                "name": step.name,
                "command": command,
                "tokens": tokens,
                "deletes": deletes,
            }
        )
    return previews


def _prune_description(argv: list[str]) -> str | None:
    """What a prune-dated step deletes, or None for any other step or a dry run."""
    if not is_prune_command(argv):
        return None
    try:
        command = prune_from_argv(argv)
    except PruneCommandError:
        return None
    return None if command.dry_run else command.description()


def _run_warning(job: JobRecord) -> str:
    """What else a real run of the whole job deletes, for its confirmation."""
    return "".join(
        f" Step {preview['name']} {preview['deletes']}."
        for preview in _command_previews(job)
        if preview["deletes"]
    )


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


def _clear_history(db) -> tuple[int, int]:
    """Delete finished run records and their logs; keep runs that are still in progress."""
    job_runs = db.query(JobRunRecord).filter(JobRunRecord.status != "running").all()
    console_runs = db.query(ConsoleRunRecord).filter(ConsoleRunRecord.status != "running").all()
    kept = (
        db.query(JobRunRecord).filter_by(status="running").count()
        + db.query(ConsoleRunRecord).filter_by(status="running").count()
    )
    job_run_ids = [run.id for run in job_runs]
    step_runs = (
        db.query(JobStepRunRecord).filter(JobStepRunRecord.job_run_id.in_(job_run_ids)).all()
        if job_run_ids
        else []
    )
    for run in [*step_runs, *console_runs]:
        _delete_log_file(Path(run.log_path))
    if job_run_ids:
        db.query(JobStepRunRecord).filter(JobStepRunRecord.job_run_id.in_(job_run_ids)).delete(
            synchronize_session=False
        )
        db.query(JobRunRecord).filter(JobRunRecord.id.in_(job_run_ids)).delete(
            synchronize_session=False
        )
    db.query(ConsoleRunRecord).filter(ConsoleRunRecord.status != "running").delete(
        synchronize_session=False
    )
    db.commit()
    return len(job_runs) + len(console_runs), kept


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


def _paginated_history(query, view: str, page: int):
    total_items = query.count()
    total_pages = max(1, ceil(total_items / HISTORY_PAGE_SIZE))
    current_page = min(max(1, page), total_pages)
    offset = (current_page - 1) * HISTORY_PAGE_SIZE
    items = query.offset(offset).limit(HISTORY_PAGE_SIZE).all()
    has_previous = current_page > 1
    has_next = current_page < total_pages
    pagination = {
        "page": current_page,
        "total_pages": total_pages,
        "total_items": total_items,
        "first_item": offset + 1 if items else 0,
        "last_item": offset + len(items),
        "has_previous": has_previous,
        "has_next": has_next,
        "previous_url": _history_url(view, current_page - 1) if has_previous else None,
        "next_url": _history_url(view, current_page + 1) if has_next else None,
    }
    return items, pagination


def _history_url(view: str, page: int = 1, *, delete_blocked: bool = False) -> str:
    params: dict[str, object] = {}
    if view != "jobs":
        params["view"] = view
    if page > 1:
        params["page"] = page
    if delete_blocked:
        # Deleting a run that is still in progress would orphan the rclone process behind it.
        params["delete_blocked"] = 1
    return "/runs" + (f"?{urlencode(params)}" if params else "")


def _history_row(view: str, record) -> dict[str, object]:
    finished = record.ended_at is not None
    row: dict[str, object] = {
        "run": record,
        "duration": _format_duration(record.started_at, record.ended_at) if finished else "-",
        "transfer": None,
    }
    if not finished:
        return row
    if view == "jobs":
        stats = _job_run_stats(record)
        row["transfer"] = _transfer_summary(
            stats.transferred_bytes,
            stats.transferred_files,
            stats.deleted_files,
            record.status,
        )
    elif view == "steps":
        stats = _step_transfer_stats(record)
        row["transfer"] = (
            _transfer_summary(
                stats.transferred_bytes,
                stats.transferred_files,
                stats.deleted_files,
                record.status,
            )
            if stats is not None
            else _transfer_summary(0, 0, 0, record.status)
        )
    return row


def _transfer_summary(
    transferred_bytes: int, transferred_files: int, deleted_files: int, status: str
) -> dict[str, str | None]:
    if not (transferred_bytes or transferred_files or deleted_files):
        # Nothing recorded: a finished successful run had nothing to do, but a failed or
        # canceled one may have stopped before rclone printed its totals.
        return {"data": "No changes" if status == "success" else "-", "detail": None}
    files = f"{transferred_files} file{'' if transferred_files == 1 else 's'}"
    detail = f"{files}, {deleted_files} deleted" if deleted_files else files
    return {"data": format_byte_count(transferred_bytes), "detail": detail}


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
    return _end_console_run_record(db, run_id, "canceled", ended_at)


def _fail_console_run_record(db, run_id: int, ended_at: datetime) -> ConsoleRunRecord | None:
    return _end_console_run_record(db, run_id, "failed", ended_at)


def _end_console_run_record(
    db, run_id: int, status: str, ended_at: datetime
) -> ConsoleRunRecord | None:
    record = db.get(ConsoleRunRecord, run_id)
    if record is None:
        return None
    record.status = status
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


def _running_now_rows(
    job_runs: list[JobRunRecord], console_runs: list[ConsoleRunRecord]
) -> list[dict[str, object]]:
    now = utc_now()
    rows: list[dict[str, object]] = []
    for job_run in job_runs:
        active_step = _active_step_run(job_run)
        rows.append(
            {
                "kind": "job",
                "name": job_run.job_name,
                "detail": f"Step: {active_step.step_name}" if active_step else "Starting",
                "mode": run_mode_label(job_run.trigger),
                "dry_run": "dry-run" in job_run.trigger,
                "started_at": job_run.started_at,
                "elapsed_seconds": _duration_seconds(job_run.started_at, now),
                "progress": _progress_text(active_step),
                "url": f"/job-runs/{job_run.id}",
            }
        )
    for console_run in console_runs:
        rows.append(
            {
                "kind": "console",
                "name": console_run.command,
                "detail": "Console command",
                "mode": "Console",
                "dry_run": False,
                "started_at": console_run.started_at,
                "elapsed_seconds": _duration_seconds(console_run.started_at, now),
                "progress": None,
                "url": f"/console/runs/{console_run.id}",
            }
        )
    return rows


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
        "progress": _progress_text(active_step_run),
        "control": runner.control_state(job_run.id) if job_run.status == "running" else None,
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
        current_step_ids = {step.id for step in job.steps} if job is not None else set()
        # Runs recorded against steps that no longer exist are shown as recorded instead.
        if job is not None and all(run.step_id in current_step_ids for run in step_runs):
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


def _live_progress(step_run: JobStepRunRecord | None) -> LiveProgress | None:
    if step_run is None or step_run.status != "running":
        return None
    return live_progress_from_log(Path(step_run.log_path))


def _progress_label(progress: LiveProgress | None) -> str | None:
    return progress.label if progress is not None else None


def _progress_text(step_run: JobStepRunRecord | None) -> str | None:
    """Live progress of a running step, marked when its run is paused."""
    progress = _live_progress(step_run)
    if step_run is None or step_run.status != "running":
        return _progress_label(progress)
    control = runner.control_state(step_run.job_run_id)
    if control is None or not control["paused_now"]:
        return _progress_label(progress)
    # rclone's speed is an average and its ETA assumes it continues, so both mislead while
    # paused; show only how far the transfer got.
    return f"Paused · {progress.amount_label}" if progress is not None else "Paused"


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
        # Deleting is refused while the step or the job around it is still running.
        "can_delete": "running" not in {run.status, run.job_run.status},
        "progress": _progress_text(run),
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
    return step_stats_display(_step_transfer_stats(step_run), step_run.status)


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
    return step_transfer_stats(step_run)


def _exit_label(status: str, exit_code: int | None) -> str:
    if exit_code is not None:
        return str(exit_code)
    if status == "canceled":
        return "Canceled"
    if status == "skipped":
        return "Skipped"
    # A run that ended without an exit code (for example, interrupted by a restart) has none.
    return "Pending" if status in {"running", "pending"} else "-"


def _console_log_path(started_at: datetime) -> Path:
    return settings.log_dir / "console" / f"{started_at.strftime('%Y%m%dT%H%M%S%fZ')}.log"


def _format_local_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local_time = value.astimezone(ZoneInfo(settings.timezone))
    return local_time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _local_time_html(value: datetime) -> Markup:
    """A table-friendly timestamp: minutes in the cell, the full time on hover."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local_time = value.astimezone(ZoneInfo(settings.timezone))
    return Markup('<time datetime="{}" title="{}">{}</time>').format(
        local_time.isoformat(timespec="seconds"),
        local_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        local_time.strftime("%Y-%m-%d %H:%M"),
    )


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


def _raw_log_response(path: Path) -> Response:
    if not path.exists():
        return Response("Log file has been pruned or is not available.", status_code=404)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)

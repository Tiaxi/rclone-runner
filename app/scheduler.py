from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.config import settings
from app.core.schedule import normalize_cron
from app.db import JobRecord, SessionLocal, record_to_job
from app.runner_service import runner

logger = logging.getLogger(__name__)

# The in-memory job store only misfires when the event loop is busy, so a late run should
# still happen rather than being dropped after APScheduler's default 1 second grace period.
MISFIRE_GRACE_SECONDS = 3600

scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))


def cron_trigger(expression: str) -> CronTrigger:
    fields = normalize_cron(expression).split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron must have 5 fields (minute hour day month weekday), got {len(fields)}."
        )
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=ZoneInfo(settings.timezone),
    )


def is_unscheduled_cron(expression: str) -> bool:
    normalized = normalize_cron(expression)
    return not normalized or normalized.lower() == "never"


def cron_error(expression: str) -> str | None:
    """Return a user-facing error for an invalid schedule, or None if it is usable."""
    if is_unscheduled_cron(expression):
        return None
    try:
        cron_trigger(expression)
    except ValueError as exc:
        return f"Invalid cron schedule: {exc}"
    return None


def next_run_time(expression: str, now: datetime | None = None) -> datetime | None:
    if is_unscheduled_cron(expression):
        return None
    if now is None:
        now = datetime.now(ZoneInfo(settings.timezone))
    try:
        trigger = cron_trigger(expression)
    except ValueError:
        return None
    return trigger.get_next_fire_time(None, now)


def sync_schedules(session: Session) -> None:
    scheduler.remove_all_jobs()
    for record in session.query(JobRecord).filter(JobRecord.enabled.is_(True)).all():
        if is_unscheduled_cron(record.cron):
            continue
        try:
            trigger = cron_trigger(record.cron)
        except ValueError as exc:
            logger.warning(
                "Not scheduling job %s: invalid cron %r: %s", record.id, record.cron, exc
            )
            continue
        scheduler.add_job(
            _run_and_record,
            trigger,
            args=[record.id],
            id=f"job-{record.id}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )


async def _run_and_record(job_id: int) -> None:
    with SessionLocal() as session:
        record = session.get(JobRecord, job_id)
        if record is None:
            return
        runner.start_job(record_to_job(record), trigger="schedule")

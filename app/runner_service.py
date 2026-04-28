from __future__ import annotations

from app.config import settings
from app.core.notifications import send_job_notification
from app.core.runner import LiveJobRunner
from app.db import SessionLocal

runner = LiveJobRunner(
    settings.log_dir,
    session_factory=SessionLocal,
    notification_callback=lambda job_run_id: send_job_notification(SessionLocal, job_run_id),
)

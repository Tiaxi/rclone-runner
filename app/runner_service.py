from __future__ import annotations

import tempfile
from pathlib import Path

from app.config import settings
from app.core.measure import SourceMeasurer
from app.core.notifications import send_job_notification
from app.core.runner import LiveJobRunner
from app.db import SessionLocal

runner = LiveJobRunner(
    settings.log_dir,
    session_factory=SessionLocal,
    notification_callback=lambda job_run_id: send_job_notification(SessionLocal, job_run_id),
    # mkdtemp makes a new directory only this user can open, under a short path: Unix socket
    # paths are limited to about 100 characters.
    control_dir=Path(tempfile.mkdtemp(prefix="rclone-runner-rc-")),
)

measurer = SourceMeasurer(SessionLocal)

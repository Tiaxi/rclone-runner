from __future__ import annotations

from app.config import settings
from app.core.runner import LiveJobRunner
from app.db import SessionLocal

runner = LiveJobRunner(settings.log_dir, session_factory=SessionLocal)

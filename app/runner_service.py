from __future__ import annotations

from app.config import settings
from app.core.runner import JobRunner

runner = JobRunner(settings.log_dir)

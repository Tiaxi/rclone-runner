from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.core.rclone_stats import RcloneTransferStats


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class JobStep:
    id: int
    name: str
    command: str
    position: int = 0


@dataclass(slots=True)
class Job:
    id: int
    name: str
    common_args: str
    env: dict[str, str]
    steps: list[JobStep] = field(default_factory=list)


@dataclass(slots=True)
class StepRunResult:
    step_id: int
    step_name: str
    argv: list[str]
    started_at: datetime
    ended_at: datetime
    exit_code: int
    log_path: Path
    transfer_stats: RcloneTransferStats | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


@dataclass(slots=True)
class JobRunResult:
    job_id: int
    job_name: str
    trigger: str
    status: str
    started_at: datetime
    ended_at: datetime
    step_runs: list[StepRunResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status == "success"

    @property
    def exit_code(self) -> int:
        for step_run in reversed(self.step_runs):
            if step_run.exit_code != 0:
                return step_run.exit_code
        return 0 if self.step_runs else -1

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

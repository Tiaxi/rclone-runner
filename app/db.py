from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import settings
from app.core.models import Job, JobRunResult, JobStep, utc_now


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cron: Mapped[str] = mapped_column(String(120), default="0 2 * * *")
    common_args: Mapped[str] = mapped_column(Text, default="")
    env_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    steps: Mapped[list[JobStepRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobStepRecord.position"
    )


class JobStepRecord(Base):
    __tablename__ = "job_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    job: Mapped[JobRecord] = relationship(back_populates="steps")


class JobRunRecord(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    job_name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    step_runs: Mapped[list[JobStepRunRecord]] = relationship(
        back_populates="job_run", cascade="all, delete-orphan"
    )


class JobStepRunRecord(Base):
    __tablename__ = "job_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_run_id: Mapped[int] = mapped_column(ForeignKey("job_runs.id"), nullable=False)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("job_steps.id"), nullable=True)
    step_name: Mapped[str] = mapped_column(String(200), nullable=False)
    argv_json: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    job_run: Mapped[JobRunRecord] = relationship(back_populates="step_runs")


class ConsoleRunRecord(Base):
    __tablename__ = "console_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    argv_json: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SettingRecord(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    with SessionLocal() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


def record_to_job(record: JobRecord) -> Job:
    env = json.loads(record.env_json or "{}")
    return Job(
        id=record.id,
        name=record.name,
        common_args=record.common_args or "",
        env={str(key): str(value) for key, value in env.items()},
        steps=[
            JobStep(id=step.id, name=step.name, command=step.command, position=step.position)
            for step in record.steps
        ],
    )


def parse_env_lines(value: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, env_value = line.partition("=")
        if not separator:
            continue
        env[key.strip()] = env_value.strip()
    return env


def env_to_lines(env_json: str) -> str:
    env = json.loads(env_json or "{}")
    return "\n".join(f"{key}={value}" for key, value in sorted(env.items()))


def read_log(path: str) -> str:
    log_path = Path(path)
    if not log_path.exists():
        return "Log file has been pruned or is not available."
    return log_path.read_text(encoding="utf-8", errors="replace")


def save_job_run(session: Session, result: JobRunResult) -> JobRunRecord:
    run_record = JobRunRecord(
        job_id=result.job_id,
        job_name=result.job_name,
        trigger=result.trigger,
        status=result.status,
        started_at=result.started_at,
        ended_at=result.ended_at,
    )
    session.add(run_record)
    session.flush()
    for step_run in result.step_runs:
        session.add(
            JobStepRunRecord(
                job_run_id=run_record.id,
                step_id=step_run.step_id,
                step_name=step_run.step_name,
                argv_json=json.dumps(step_run.argv),
                exit_code=step_run.exit_code,
                log_path=str(step_run.log_path),
                started_at=step_run.started_at,
                ended_at=step_run.ended_at,
            )
        )
    session.commit()
    session.refresh(run_record)
    return run_record

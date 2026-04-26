from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, inspect
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
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="success")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_run: Mapped[JobRunRecord] = relationship(back_populates="step_runs")


class ConsoleRunRecord(Base):
    __tablename__ = "console_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="success")
    command: Mapped[str] = mapped_column(Text, nullable=False)
    argv_json: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    _migrate_lifecycle_columns()


def _migrate_lifecycle_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if engine.dialect.name != "sqlite":
        return
    rebuild_tables = [
        table
        for table in ("job_runs", "job_step_runs", "console_runs")
        if table in existing_tables and _needs_lifecycle_rebuild(inspector, table)
    ]
    if "job_runs" in rebuild_tables and "job_step_runs" in existing_tables:
        rebuild_tables = [table for table in rebuild_tables if table != "job_step_runs"]
        rebuild_tables.insert(1, "job_step_runs")
    if not rebuild_tables:
        return

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            for table in rebuild_tables:
                connection.exec_driver_sql(f"ALTER TABLE {table} RENAME TO {table}_old")
            for table in rebuild_tables:
                Base.metadata.tables[table].create(bind=connection)
            for table in rebuild_tables:
                _copy_lifecycle_rows(connection, inspector, table)
                connection.exec_driver_sql(f"DROP TABLE {table}_old")
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _needs_lifecycle_rebuild(inspector, table: str) -> bool:
    columns = {column["name"]: column for column in inspector.get_columns(table)}
    if table == "job_runs":
        return bool(columns.get("ended_at", {}).get("nullable") is False)
    if table in {"job_step_runs", "console_runs"}:
        return (
            "status" not in columns
            or columns.get("exit_code", {}).get("nullable") is False
            or columns.get("ended_at", {}).get("nullable") is False
        )
    return False


def _copy_lifecycle_rows(connection, inspector, table: str) -> None:
    old_columns = {column["name"] for column in inspector.get_columns(f"{table}_old")}
    status_expr = _lifecycle_status_expr(old_columns)
    if table == "job_runs":
        connection.exec_driver_sql(
            """
            INSERT INTO job_runs (id, job_id, job_name, trigger, status, started_at, ended_at)
            SELECT id, job_id, job_name, trigger, status, started_at, ended_at
            FROM job_runs_old
            """
        )
    elif table == "job_step_runs":
        connection.exec_driver_sql(
            f"""
            INSERT INTO job_step_runs (
                id, job_run_id, step_id, step_name, argv_json, status, exit_code, log_path,
                started_at, ended_at
            )
            SELECT
                id, job_run_id, step_id, step_name, argv_json, {status_expr}, exit_code, log_path,
                started_at, ended_at
            FROM job_step_runs_old
            """
        )
    elif table == "console_runs":
        connection.exec_driver_sql(
            f"""
            INSERT INTO console_runs (
                id, status, command, argv_json, exit_code, log_path, started_at, ended_at
            )
            SELECT
                id, {status_expr}, command, argv_json, exit_code, log_path, started_at, ended_at
            FROM console_runs_old
            """
        )


def _lifecycle_status_expr(old_columns: set[str]) -> str:
    if "status" in old_columns:
        return "status"
    if "exit_code" in old_columns:
        return "CASE WHEN exit_code = 0 THEN 'success' ELSE 'failed' END"
    return "'success'"


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

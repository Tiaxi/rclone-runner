from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.scheduler as scheduler_module
from app.config import settings
from app.core.schedule import cron_summary, normalize_cron
from app.db import Base, JobRecord


def test_empty_cron_is_never():
    assert normalize_cron("  ") == ""
    assert cron_summary("  ") == "Never"


def test_next_run_time_returns_none_for_never_schedule():
    assert scheduler_module.next_run_time("") is None
    assert scheduler_module.next_run_time("never") is None


def test_next_run_time_returns_future_time_for_daily_schedule():
    timezone = ZoneInfo(settings.timezone)
    now = datetime(2026, 4, 28, 1, 0, tzinfo=timezone)
    next_run = scheduler_module.next_run_time("0 2 * * *", now=now)

    assert next_run == datetime(2026, 4, 28, 2, 0, tzinfo=timezone)


def test_common_cron_summaries_are_humanized():
    assert cron_summary("0 2 * * *") == "Daily at 02:00"
    assert cron_summary("30 3 * * 1") == "Weekly on Monday at 03:30"
    assert cron_summary("0 4 1 * *") == "Monthly on day 1 at 04:00"
    assert cron_summary("*/15 * * * *") == "Every 15 minutes"
    assert cron_summary("0 */6 * * *") == "Every 6 hours"


def test_sync_schedules_registers_async_job_runner(tmp_path: Path, monkeypatch):
    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs = []

        def remove_all_jobs(self) -> None:
            self.jobs.clear()

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    session_factory = _session_factory(tmp_path / "runs.db")
    with session_factory() as session:
        session.add(JobRecord(name="backup", cron="*/1 * * * *", enabled=True))
        session.commit()

        scheduler_module.sync_schedules(session)

    assert fake_scheduler.jobs[0]["func"] is scheduler_module._run_and_record


def test_sync_schedules_skips_never_schedule(tmp_path: Path, monkeypatch):
    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs = []

        def remove_all_jobs(self) -> None:
            self.jobs.clear()

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    session_factory = _session_factory(tmp_path / "runs.db")
    with session_factory() as session:
        session.add(JobRecord(name="manual", cron="never", enabled=True))
        session.commit()

        scheduler_module.sync_schedules(session)

    assert fake_scheduler.jobs == []


def test_cron_error_reports_invalid_schedules():
    assert scheduler_module.cron_error("") is None
    assert scheduler_module.cron_error("never") is None
    assert scheduler_module.cron_error("0 5 * * *") is None
    assert "higher than the maximum" in scheduler_module.cron_error("0 25 * * *")
    assert "5 fields" in scheduler_module.cron_error("0 0 5 * * *")


def test_sync_schedules_skips_invalid_cron_and_keeps_other_jobs(tmp_path: Path, monkeypatch):
    class FakeScheduler:
        def __init__(self) -> None:
            self.jobs = []

        def remove_all_jobs(self) -> None:
            self.jobs.clear()

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)
    session_factory = _session_factory(tmp_path / "runs.db")
    with session_factory() as session:
        session.add(JobRecord(name="broken", cron="0 25 * * *", enabled=True))
        session.add(JobRecord(name="nightly", cron="0 5 * * *", enabled=True))
        session.commit()

        scheduler_module.sync_schedules(session)

    assert [job["kwargs"]["id"] for job in fake_scheduler.jobs] == ["job-2"]
    assert fake_scheduler.jobs[0]["kwargs"]["misfire_grace_time"] == 3600


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


FIXED_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo(settings.timezone))  # a Monday


def _next_weekdays(expression: str, count: int = 7) -> list[str]:
    trigger = scheduler_module.cron_trigger(expression)
    fire_times = []
    previous = None
    now = FIXED_NOW
    for _ in range(count):
        fire_time = trigger.get_next_fire_time(previous, now)
        fire_times.append(fire_time.strftime("%a"))
        previous = fire_time
        now = fire_time
    return fire_times


def test_numeric_weekdays_follow_cron_numbering():
    # The summary and the actual fire dates agree: 0 and 7 are Sunday, 1 is Monday.
    assert cron_summary("0 2 * * 0") == "Weekly on Sunday at 02:00"
    assert _next_weekdays("0 2 * * 0", 2) == ["Sun", "Sun"]
    assert cron_summary("0 2 * * 7") == "Weekly on Sunday at 02:00"
    assert _next_weekdays("0 2 * * 7", 1) == ["Sun"]
    assert cron_summary("0 2 * * 1") == "Weekly on Monday at 02:00"
    assert scheduler_module.next_run_time("0 2 * * 1", now=FIXED_NOW) == datetime(
        2026, 11, 23, 2, 0, tzinfo=FIXED_NOW.tzinfo
    )


def test_weekday_ranges_lists_and_steps_follow_cron_numbering():
    assert _next_weekdays("0 2 * * 1-5", 5) == ["Tue", "Wed", "Thu", "Fri", "Mon"]
    assert cron_summary("0 2 * * 1-5") == "Weekly on Monday–Friday at 02:00"
    # In cron, */2 is Sunday, Tuesday, Thursday and Saturday.
    assert _next_weekdays("0 2 * * */2", 4) == ["Tue", "Thu", "Sat", "Sun"]
    assert cron_summary("0 2 * * */2") == (
        "Weekly on Tuesday, Thursday, Saturday and Sunday at 02:00"
    )
    assert _next_weekdays("0 2 * * 0,6", 2) == ["Sat", "Sun"]
    assert cron_summary("0 2 * * 0,6") == "Weekly on Saturday and Sunday at 02:00"
    assert _next_weekdays("0 2 * * 5-7", 3) == ["Fri", "Sat", "Sun"]
    assert _next_weekdays("0 2 * * 1-5/2", 3) == ["Wed", "Fri", "Mon"]
    assert cron_summary("0 2 * * 0-6") == "Weekly on every day at 02:00"


def test_named_weekdays_keep_working():
    assert _next_weekdays("0 2 * * mon-fri", 5) == ["Tue", "Wed", "Thu", "Fri", "Mon"]
    assert _next_weekdays("0 2 * * SUN", 1) == ["Sun"]
    assert _next_weekdays("0 2 * * fri-sun", 3) == ["Fri", "Sat", "Sun"]
    assert cron_summary("0 2 * * sat,sun") == "Weekly on Saturday and Sunday at 02:00"


def test_invalid_weekdays_are_reported():
    assert "not a weekday" in scheduler_module.cron_error("0 2 * * 8")
    assert "not a weekday" in scheduler_module.cron_error("0 2 * * funday")
    assert "backwards" in scheduler_module.cron_error("0 2 * * 5-1")
    assert cron_summary("0 2 * * 8") == "Custom cron: 0 2 * * 8"


def test_day_of_month_and_weekday_together_are_refused():
    # cron runs on either day; APScheduler would only run when both match.
    assert "not both" in scheduler_module.cron_error("0 2 1 * 1")
    assert "not both" in scheduler_module.cron_error("0 2 1-7 * mon")
    # A field starting with * leaves cron matching both, which APScheduler does too.
    assert scheduler_module.cron_error("0 2 */2 * 1") is None
    assert scheduler_module.cron_error("0 2 1 * */2") is None

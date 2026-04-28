from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.notifications import (
    EmailNotificationSettings,
    build_job_notification_message,
    load_email_notification_settings,
    notification_enabled_for_status,
    parse_recipients,
    read_log_tail,
    save_email_notification_settings,
    send_job_notification,
)
from app.db import Base, JobRunRecord, JobStepRunRecord


def test_email_notification_settings_round_trip_preserves_blank_password(tmp_path):
    session_factory = _session_factory(tmp_path / "runs.db")
    with session_factory() as db:
        save_email_notification_settings(
            db,
            EmailNotificationSettings(
                enabled=True,
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username="user@gmail.com",
                smtp_password="app-password",
                sender="user@gmail.com",
                recipients="ops@example.com\nadmin@example.com",
                notify_success=True,
                notify_failure=True,
                notify_canceled=False,
                app_base_url="http://rclone-runner.local",
                include_log_tail_lines=150,
            ),
        )
        save_email_notification_settings(
            db,
            EmailNotificationSettings(
                enabled=False,
                smtp_host="smtp.example.com",
                smtp_port=2525,
                smtp_username="other@example.com",
                smtp_password="",
                sender="sender@example.com",
                recipients="ops@example.com",
                notify_success=False,
                notify_failure=True,
                notify_canceled=True,
                app_base_url="",
                include_log_tail_lines=25,
            ),
            preserve_password=True,
        )

        loaded = load_email_notification_settings(db)

    assert loaded.smtp_password == "app-password"
    assert loaded.smtp_host == "smtp.example.com"
    assert loaded.enabled is False
    assert loaded.include_log_tail_lines == 25


def test_parse_recipients_accepts_commas_and_newlines():
    assert parse_recipients("one@example.com, two@example.com\nthree@example.com") == [
        "one@example.com",
        "two@example.com",
        "three@example.com",
    ]


def test_notification_enabled_for_status_respects_event_toggles():
    settings = EmailNotificationSettings(
        enabled=True,
        notify_success=False,
        notify_failure=True,
        notify_canceled=True,
    )

    assert not notification_enabled_for_status(settings, "success")
    assert notification_enabled_for_status(settings, "failed")
    assert notification_enabled_for_status(settings, "canceled")
    assert not notification_enabled_for_status(settings, "skipped")


def test_read_log_tail_limits_lines(tmp_path):
    log_path = tmp_path / "run.log"
    log_path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    assert read_log_tail(log_path, 2) == "three\nfour\n"


def test_job_notification_message_contains_html_summary_and_failure_log(tmp_path):
    log_path = tmp_path / "run.log"
    log_path.write_text("line one\nline two\n", encoding="utf-8")
    run = JobRunRecord(
        id=7,
        job_id=3,
        job_name="Backup",
        trigger="schedule",
        status="failed",
        started_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 28, 10, 5, tzinfo=UTC),
    )
    run.step_runs = [
        JobStepRunRecord(
            id=9,
            job_run_id=7,
            step_id=1,
            step_name="Sync",
            argv_json='["rclone", "sync"]',
            status="failed",
            exit_code=1,
            log_path=str(log_path),
            started_at=run.started_at,
            ended_at=run.ended_at,
        )
    ]
    settings = EmailNotificationSettings(
        sender="rclone@example.com",
        recipients="ops@example.com",
        app_base_url="http://runner.local",
        include_log_tail_lines=20,
    )

    message = build_job_notification_message(settings, run, "line one\nline two\n")
    html = message.get_body(("html",)).get_content()
    text = message.get_body(("plain",)).get_content()

    assert message["Subject"] == "[Rclone Runner] FAILED: Backup"
    assert message["From"] == "rclone@example.com"
    assert message["To"] == "ops@example.com"
    assert "Backup" in html
    assert "FAILED" in html
    assert "Rclone Runner" in html
    assert "Summary" in html
    assert "background: #fde2df" in html
    assert "border-radius: 12px" in html
    assert 'role="presentation"' in html
    assert "Sync" in html
    assert "Log tail" in html
    assert "font-family: Consolas, monospace" in html
    assert "line two" in html
    assert "http://runner.local/job-runs/7" in html
    assert "line two" in text


async def test_send_job_notification_uses_stored_settings_and_failure_log(tmp_path):
    sent = []
    session_factory = _session_factory(tmp_path / "runs.db")
    log_path = tmp_path / "run.log"
    log_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
    with session_factory() as db:
        save_email_notification_settings(
            db,
            EmailNotificationSettings(
                enabled=True,
                smtp_host="smtp.gmail.com",
                smtp_username="user@gmail.com",
                smtp_password="app-password",
                sender="user@gmail.com",
                recipients="ops@example.com",
                notify_failure=True,
                include_log_tail_lines=2,
            ),
        )
        run = JobRunRecord(
            job_id=3,
            job_name="Backup",
            trigger="manual",
            status="failed",
            started_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 4, 28, 10, 5, tzinfo=UTC),
        )
        db.add(run)
        db.flush()
        db.add(
            JobStepRunRecord(
                job_run_id=run.id,
                step_id=1,
                step_name="Sync",
                argv_json='["rclone", "sync"]',
                status="failed",
                exit_code=1,
                log_path=str(log_path),
                started_at=run.started_at,
                ended_at=run.ended_at,
            )
        )
        db.commit()
        run_id = run.id

    def fake_sender(settings, message):
        sent.append((settings, message))

    await send_job_notification(session_factory, run_id, sender=fake_sender)

    assert len(sent) == 1
    message = sent[0][1]
    html = message.get_body(("html",)).get_content()
    assert "second" in html
    assert "third" in html
    assert "first" not in html


async def test_send_job_notification_skips_disabled_status(tmp_path):
    sent = []
    session_factory = _session_factory(tmp_path / "runs.db")
    with session_factory() as db:
        save_email_notification_settings(
            db,
            EmailNotificationSettings(
                enabled=True,
                smtp_host="smtp.gmail.com",
                sender="user@gmail.com",
                recipients="ops@example.com",
                notify_success=False,
            ),
        )
        run = JobRunRecord(
            job_id=3,
            job_name="Backup",
            trigger="manual",
            status="success",
            started_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 4, 28, 10, 5, tzinfo=UTC),
        )
        db.add(run)
        db.commit()
        run_id = run.id

    await send_job_notification(
        session_factory,
        run_id,
        sender=lambda settings, message: sent.append(message),
    )

    assert sent == []


def _session_factory(database_path: Path):
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

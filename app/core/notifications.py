from __future__ import annotations

import asyncio
import html
import json
import logging
import smtplib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.core.rclone_stats import (
    RcloneTransferStats,
    RunStatsDisplay,
    run_stats_display,
    stats_from_json,
    stats_from_log,
    step_stats_display,
)
from app.db import JobRunRecord, SettingRecord

EMAIL_SETTINGS_KEY = "email_notifications"
DEFAULT_SMTP_PORT = 587
DEFAULT_LOG_TAIL_LINES = 200
SMTP_TIMEOUT_SECONDS = 20

logger = logging.getLogger(__name__)


@dataclass
class EmailNotificationSettings:
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_username: str = ""
    smtp_password: str = ""
    sender: str = ""
    recipients: str = ""
    use_starttls: bool = True
    notify_success: bool = False
    notify_failure: bool = True
    notify_canceled: bool = True
    app_base_url: str = ""
    include_log_tail_lines: int = DEFAULT_LOG_TAIL_LINES


def load_email_notification_settings(db: Session) -> EmailNotificationSettings:
    record = db.get(SettingRecord, EMAIL_SETTINGS_KEY)
    if record is None:
        return EmailNotificationSettings()
    try:
        raw = json.loads(record.value)
    except json.JSONDecodeError:
        logger.warning("Invalid email notification settings JSON")
        return EmailNotificationSettings()
    defaults = asdict(EmailNotificationSettings())
    values = defaults | {key: raw[key] for key in defaults.keys() & raw.keys()}
    values["smtp_port"] = _positive_int(values["smtp_port"], DEFAULT_SMTP_PORT)
    values["include_log_tail_lines"] = _positive_int(
        values["include_log_tail_lines"], DEFAULT_LOG_TAIL_LINES
    )
    return EmailNotificationSettings(**values)


def save_email_notification_settings(
    db: Session, value: EmailNotificationSettings, preserve_password: bool = False
) -> None:
    if preserve_password and not value.smtp_password:
        value.smtp_password = load_email_notification_settings(db).smtp_password
    value.smtp_port = _positive_int(value.smtp_port, DEFAULT_SMTP_PORT)
    value.include_log_tail_lines = _positive_int(
        value.include_log_tail_lines, DEFAULT_LOG_TAIL_LINES
    )
    payload = json.dumps(asdict(value), sort_keys=True)
    record = db.get(SettingRecord, EMAIL_SETTINGS_KEY)
    if record is None:
        db.add(SettingRecord(key=EMAIL_SETTINGS_KEY, value=payload))
    else:
        record.value = payload
    db.commit()


def parse_recipients(value: str) -> list[str]:
    normalized = value.replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def notification_enabled_for_status(value: EmailNotificationSettings, status: str) -> bool:
    if not value.enabled:
        return False
    if status == "success":
        return value.notify_success
    if status == "failed":
        return value.notify_failure
    if status == "canceled":
        return value.notify_canceled
    return False


def read_log_tail(path: Path, line_count: int) -> str:
    if line_count <= 0 or not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[-line_count:])


def build_job_notification_message(
    value: EmailNotificationSettings, run: JobRunRecord, log_tail: str = ""
) -> EmailMessage:
    recipients = parse_recipients(value.recipients)
    status_label = run.status.upper()
    subject = f"[Rclone Runner] {status_label}: {run.job_name}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = value.sender or value.smtp_username
    message["To"] = ", ".join(recipients)
    text = _plain_text_body(value, run, log_tail)
    html_body = _html_body(value, run, log_tail)
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")
    return message


async def send_job_notification(
    session_factory: Callable[[], Session],
    job_run_id: int,
    sender: Callable[[EmailNotificationSettings, EmailMessage], None] | None = None,
) -> None:
    try:
        with session_factory() as db:
            notification_settings = load_email_notification_settings(db)
            run = db.get(JobRunRecord, job_run_id)
            if run is None or not notification_enabled_for_status(
                notification_settings, run.status
            ):
                return
            if not _settings_ready(notification_settings):
                logger.warning("Email notifications enabled but SMTP settings are incomplete")
                return
            log_tail = _job_log_tail(run, notification_settings.include_log_tail_lines)
            message = build_job_notification_message(notification_settings, run, log_tail)
        if sender is not None:
            sender(notification_settings, message)
        else:
            await asyncio.to_thread(send_smtp_message, notification_settings, message)
    except Exception:
        logger.exception("Failed to send job notification email")


async def send_test_notification(
    value: EmailNotificationSettings,
    sender: Callable[[EmailNotificationSettings, EmailMessage], None] | None = None,
) -> None:
    if not _settings_ready(value):
        raise ValueError("Email notification settings are incomplete")
    message = EmailMessage()
    message["Subject"] = "[Rclone Runner] Test email"
    message["From"] = value.sender or value.smtp_username
    message["To"] = ", ".join(parse_recipients(value.recipients))
    message.set_content("Rclone Runner email notifications are configured.")
    message.add_alternative(
        "<h1>Rclone Runner</h1><p>Email notifications are configured.</p>",
        subtype="html",
    )
    if sender is not None:
        sender(value, message)
    else:
        await asyncio.to_thread(send_smtp_message, value, message)


def send_smtp_message(value: EmailNotificationSettings, message: EmailMessage) -> None:
    with smtplib.SMTP(value.smtp_host, value.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        if value.use_starttls:
            smtp.starttls()
        if value.smtp_username or value.smtp_password:
            smtp.login(value.smtp_username, value.smtp_password)
        smtp.send_message(message)


def _settings_ready(value: EmailNotificationSettings) -> bool:
    return bool(
        value.smtp_host
        and value.smtp_port
        and (value.sender or value.smtp_username)
        and parse_recipients(value.recipients)
    )


def _job_log_tail(run: JobRunRecord, line_count: int) -> str:
    if run.status not in {"failed", "canceled"}:
        return ""
    candidates = [
        step for step in run.step_runs if step.status in {"failed", "canceled"} and step.log_path
    ] or [step for step in run.step_runs if step.log_path]
    if not candidates:
        return ""
    latest_step = max(candidates, key=lambda step: step.started_at)
    return read_log_tail(Path(latest_step.log_path), line_count)


def _plain_text_body(value: EmailNotificationSettings, run: JobRunRecord, log_tail: str) -> str:
    run_stats = _job_run_stats(run)
    lines = [
        f"{run.status.upper()}: {run.job_name}",
        "",
        f"Mode: {_run_mode_label(run.trigger)}",
        f"Started: {_format_time(run.started_at)}",
        f"Finished: {_format_time(run.ended_at)}",
        f"Duration: {_format_duration(run.started_at, run.ended_at)}",
        f"Data transferred: {run_stats.transferred_data_label}",
        f"Files transferred: {run_stats.transferred_files_label}",
        f"Deleted files: {run_stats.deleted_files_label}",
    ]
    if run_stats.has_unavailable:
        lines.append("Some step stats unavailable")
    if value.app_base_url:
        lines.append(f"Run: {value.app_base_url.rstrip('/')}/job-runs/{run.id}")
    lines.append("")
    lines.append("Steps:")
    for step in sorted(run.step_runs, key=lambda item: item.started_at):
        stats = step_stats_display(_step_transfer_stats(step))
        step_duration = _format_duration(step.started_at, step.ended_at)
        lines.append(
            f"- {step.step_name}: {step.status.upper()} "
            f"(exit {_exit_label(step.exit_code, step.status)}, {step_duration}), {stats.label}"
        )
    if log_tail:
        lines.extend(["", "Log tail:", log_tail])
    return "\n".join(lines)


def _html_body(value: EmailNotificationSettings, run: JobRunRecord, log_tail: str) -> str:
    run_stats = _job_run_stats(run)
    status = html.escape(run.status.upper())
    status_bg, status_text = _status_colors(run.status)
    job_name = html.escape(run.job_name)
    duration = html.escape(_format_duration(run.started_at, run.ended_at))
    body_style = (
        "background: #f5f7f2; color: #20231f; "
        "font-family: Inter, Arial, sans-serif; margin: 0; padding: 24px;"
    )
    card_style = (
        "background: #ffffff; border: 1px solid #dbe3d7; border-radius: 12px; overflow: hidden;"
    )
    eyebrow_style = "font-size: 13px; font-weight: 700; letter-spacing: 0;"
    badge_style = (
        f"background: {status_bg}; background-color: {status_bg}; "
        f"background-image: linear-gradient({status_bg}, {status_bg}); "
        f"border: 1px solid {status_text}; border-radius: 999px; color: {status_text} !important; "
        f"-webkit-text-fill-color: {status_text}; display: inline-block; font-size: 12px; "
        "font-weight: 800; padding: 6px 10px;"
    )
    brand_mark_style = (
        "background: #2f6f4e; background-color: #2f6f4e; "
        "background-image: linear-gradient(#2f6f4e, #2f6f4e); "
        "border: 1px solid #6fbd8f; border-radius: 7px; color: #f7f7f4 !important; "
        "-webkit-text-fill-color: #f7f7f4; display: inline-block; "
        "font-family: Consolas, monospace; font-size: 13px; font-weight: 800; "
        "line-height: 24px; text-align: center; width: 24px;"
    )
    brand_label_style = (
        f"{eyebrow_style}; padding: 0; text-transform: uppercase; vertical-align: middle;"
    )
    steps_table_style = (
        "border: 1px solid #dbe3d7; border-collapse: collapse; "
        "border-radius: 8px; overflow: hidden; width: 100%;"
    )
    steps_header_style = (
        "background: #eef3eb; background-color: #eef3eb; "
        "background-image: linear-gradient(#eef3eb, #eef3eb); color: #20231f !important; "
        "-webkit-text-fill-color: #20231f;"
    )
    rows = "\n".join(
        _step_row_html(step) for step in sorted(run.step_runs, key=lambda item: item.started_at)
    )
    link = ""
    if value.app_base_url:
        url = f"{value.app_base_url.rstrip('/')}/job-runs/{run.id}"
        link = (
            '<p style="margin: 20px 0 0;">'
            f'<a href="{html.escape(url)}" '
            'style="background: #2f6f4e; background-color: #2f6f4e; '
            "background-image: linear-gradient(#2f6f4e, #2f6f4e); border: 1px solid #6fbd8f; "
            "border-radius: 6px; color: #ffffff !important; display: inline-block; "
            "font-weight: 700; padding: 10px 14px; text-decoration: none; "
            '-webkit-text-fill-color: #ffffff;">Open run</a></p>'
        )
    summary_rows = "\n".join(
        [
            _summary_row("Mode", _run_mode_label(run.trigger), include_width=True),
            _summary_row("Started", _format_time(run.started_at)),
            _summary_row("Finished", _format_time(run.ended_at)),
            _summary_row("Duration", duration, escape_value=False),
            _summary_row("Data transferred", run_stats.transferred_data_label),
            _summary_row("Files transferred", run_stats.transferred_files_label),
            _summary_row("Deleted files", run_stats.deleted_files_label),
        ]
    )
    log_block = ""
    if log_tail:
        log_block = (
            '<h2 style="font-size: 18px; margin: 28px 0 10px;">Log tail</h2>'
            '<pre style="background: #111510; border-radius: 8px; color: #eef5ea; '
            "font-family: Consolas, monospace; font-size: 13px; line-height: 1.45; "
            f'margin: 0; overflow-x: auto; padding: 14px;">{html.escape(log_tail)}</pre>'
        )
    return f"""<!doctype html>
<html>
  <body style="{body_style}">
    <div style="margin: 0 auto; max-width: 680px;">
      <div style="{card_style}">
        <div style="background: #1a1f18; color: #edf2e9; padding: 22px 24px;">
          <table role="presentation" style="border-collapse: collapse; margin-bottom: 16px;">
            <tr>
              <td style="padding: 0 10px 0 0; vertical-align: middle;">
                <span class="brand-mark" style="{brand_mark_style}">&gt;_</span>
              </td>
              <td style="{brand_label_style}">
                Rclone Runner
              </td>
            </tr>
          </table>
          <table role="presentation" style="border-collapse: collapse; width: 100%;">
            <tr>
              <td style="padding: 0 12px 0 0; vertical-align: middle;">
                <h1 style="font-size: 26px; line-height: 1.25; margin: 0;">{job_name}</h1>
              </td>
              <td align="right" style="padding: 0; vertical-align: middle; white-space: nowrap;">
                <span style="{badge_style}">{status}</span>
              </td>
            </tr>
          </table>
        </div>
        <div style="padding: 24px;">
          <h2 style="font-size: 18px; margin: 0 0 10px;">Summary</h2>
          <table role="presentation" style="border-collapse: collapse; width: 100%;">
            {summary_rows}
          </table>
    {link}
          <h2 style="font-size: 18px; margin: 28px 0 10px;">Steps</h2>
          <table style="{steps_table_style}">
            <thead>
              <tr style="{steps_header_style}">
                <th align="left" style="padding: 12px 14px;">Step</th>
                <th align="left" style="padding: 12px 14px;">Status</th>
                <th align="left" style="padding: 12px 14px;">Exit</th>
                <th align="left" style="padding: 12px 14px;">Stats</th>
                <th align="left" style="padding: 12px 14px;">Started</th>
                <th align="left" style="padding: 12px 14px;">Duration</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
          {log_block}
        </div>
      </div>
    </div>
  </body>
</html>"""


def _step_row_html(step) -> str:
    stats = step_stats_display(_step_transfer_stats(step))
    duration = _format_duration(step.started_at, step.ended_at)
    return (
        "<tr>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7;">'
        f"{html.escape(step.step_name)}</td>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7; '
        'font-weight: 700;">'
        f"{html.escape(step.status.upper())}</td>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7;">'
        f"{html.escape(_exit_label(step.exit_code, step.status))}</td>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7;">'
        f"{html.escape(stats.label)}</td>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7;">'
        f"{html.escape(_format_time(step.started_at))}</td>"
        '<td style="padding: 12px 14px; border-top: 1px solid #dbe3d7;">'
        f"{html.escape(duration)}</td>"
        "</tr>"
    )


def _job_run_stats(run: JobRunRecord) -> RunStatsDisplay:
    values: list[RcloneTransferStats | None] = []
    for step in run.step_runs:
        if step.status == "running" or step.ended_at is None:
            continue
        stats = _step_transfer_stats(step)
        values.append(stats)
    return run_stats_display(values)


def _step_transfer_stats(step) -> RcloneTransferStats | None:
    stored = stats_from_json(step.transfer_stats_json)
    if stored is not None:
        return stored
    if step.ended_at is None:
        return None
    return stats_from_log(Path(step.log_path))


def _status_colors(status: str) -> tuple[str, str]:
    if status == "success":
        return "#dff4e6", "#1f6f3d"
    if status == "failed":
        return "#fde2df", "#9f2f28"
    if status == "canceled":
        return "#fff1cc", "#755200"
    return "#edf0ea", "#20231f"


def _summary_row(
    label: str, value: str, *, include_width: bool = False, escape_value: bool = True
) -> str:
    width = " width: 120px;" if include_width else ""
    display_value = html.escape(value) if escape_value else value
    return (
        "<tr>"
        '<th align="left" style="border-top: 1px solid #dbe3d7; '
        f'color: #62685f; padding: 10px 0;{width}">{html.escape(label)}</th>'
        '<td style="border-top: 1px solid #dbe3d7; padding: 10px 0;">'
        f"{display_value}</td>"
        "</tr>"
    )


def _run_mode_label(trigger: str) -> str:
    if "dry-run" in trigger:
        return "Dry run"
    if trigger == "schedule":
        return "Scheduled"
    if "step" in trigger:
        return "Step run"
    return "Run"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "Running"
    return value.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_duration(started_at: datetime, ended_at: datetime | None) -> str:
    if ended_at is None:
        return "Running"
    total_seconds = max(0, int((ended_at - started_at).total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _exit_label(exit_code: int | None, status: str) -> str:
    if status == "canceled":
        return "Canceled"
    if exit_code is None:
        return "Pending"
    return str(exit_code)


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return default
    return parsed if parsed > 0 else default

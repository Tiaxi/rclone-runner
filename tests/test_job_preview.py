import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from app.db import JobRecord, JobStepRecord
from app.main import _command_previews, templates


def _test_csrf_field() -> str:
    return '<input type="hidden" name="csrf_token" value="token">'


templates.env.globals["csrf_field"] = _test_csrf_field


def test_headings_after_large_blocks_have_section_spacing():
    css = Path("app/static/styles.css").read_text()

    assert ".table-wrap + h2" in css
    assert ".history-section + .history-section" in css
    assert ".form-grid + h2" in css
    assert ".terminal-output + h2" in css
    assert ".section-heading" in css


def test_base_template_links_svg_favicon():
    html = templates.get_template("base.html").render()
    login_html = templates.get_template("login.html").render()

    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in html
    assert '<img class="brand-mark" src="/static/favicon.svg" alt="" aria-hidden="true">' in html
    assert (
        '<img class="brand-mark" src="/static/favicon.svg" alt="" aria-hidden="true">' in login_html
    )
    assert 'class="login-brand"' in login_html


def test_base_template_supports_theme_detection_and_toggle():
    html = templates.get_template("base.html").render()
    login_html = templates.get_template("login.html").render()
    css = Path("app/static/styles.css").read_text()

    assert 'localStorage.getItem("theme")' in html
    assert 'localStorage.getItem("theme")' in login_html
    assert 'matchMedia("(prefers-color-scheme: dark)")' in html
    assert 'matchMedia("(prefers-color-scheme: dark)")' in login_html
    assert "document.documentElement.dataset.theme = theme;" in html
    assert "document.documentElement.dataset.theme = theme;" in login_html
    assert 'id="theme-toggle"' in html
    assert 'aria-label="Switch color theme"' in html
    assert 'localStorage.setItem("theme", nextTheme)' in html
    assert 'html[data-theme="dark"]' in css
    assert "color-scheme: dark" in css


def test_base_template_renders_runtime_warnings():
    html = templates.get_template("base.html").render(
        runtime_warnings=["RCLONE_RUNNER_SECRET_KEY uses a default session secret."]
    )
    css = Path("app/static/styles.css").read_text()

    assert 'class="runtime-warning"' in html
    assert 'role="alert"' in html
    assert "RCLONE_RUNNER_SECRET_KEY uses a default session secret." in html
    assert ".runtime-warning" in css


def test_compose_declares_healthcheck():
    compose = Path("docker-compose.yml").read_text()

    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8000/health" in compose


def test_readme_includes_deployment_checklist_and_backup_guidance():
    readme = Path("README.md").read_text()

    assert "## Deployment Checklist" in readme
    assert "Email Notifications" in readme
    assert "smtp.gmail.com" in readme
    assert "RCLONE_RUNNER_ADMIN_PASSWORD_HASH" in readme
    assert "RCLONE_RUNNER_SECRET_KEY" in readme
    assert "/health" in readme
    assert "Back up" in readme
    assert "rclone.conf" in readme


def test_jobs_page_renders_ongoing_activity_sections():
    started_at = datetime(2026, 4, 26, 18, 0, tzinfo=UTC)
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="running",
        started_at=started_at,
        ended_at=None,
    )
    step_run = SimpleNamespace(
        id=4,
        step_name="Music",
        status="running",
        started_at=started_at,
        ended_at=None,
        job_run=SimpleNamespace(job_name="backup", trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=5,
        command="lsd remote:",
        status="running",
        started_at=started_at,
        ended_at=None,
    )

    html = templates.get_template("jobs.html").render(
        jobs=[],
        ongoing_job_runs=[{"run": job_run, "current_step": "Music"}],
        ongoing_step_runs=[step_run],
        ongoing_console_runs=[console_run],
    )

    assert "<h2>Ongoing activity</h2>" in html
    assert "<h3>Job runs</h3>" in html
    assert '<a href="/job-runs/3">Open</a>' in html
    assert "<h3>Step runs</h3>" in html
    assert '<a href="/runs/4">Log</a>' in html
    assert "<h3>Console commands</h3>" in html
    assert '<a href="/console/runs/5">Log</a>' in html
    assert "2026-04-26 21:00:00 EEST" in html


def test_jobs_page_renders_empty_ongoing_activity_tables():
    html = templates.get_template("jobs.html").render(
        jobs=[],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
    )

    assert "<h2>Ongoing activity</h2>" in html
    assert "<h3>Job runs</h3>" in html
    assert "No job runs are currently active." in html
    assert "<h3>Step runs</h3>" in html
    assert "No step runs are currently active." in html
    assert "<h3>Console commands</h3>" in html
    assert "No console commands are currently active." in html


def test_job_detail_history_heading_has_explicit_section_spacing():
    html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[],
        runs=[],
    )

    assert '<h2 class="section-heading">Recent job runs</h2>' in html


def test_command_preview_uses_common_args_and_job_environment():
    job = JobRecord(
        name="backup",
        cron="",
        common_args="--fast-list --bwlimit ${BW_LIMIT}",
        env_json=json.dumps({"BW_LIMIT": "8M"}),
    )
    job.steps = [
        JobStepRecord(position=1, name="Music", command="sync /media/Musiikki secret:/Musiikki")
    ]

    previews = _command_previews(job)

    assert previews == [
        {
            "id": None,
            "name": "Music",
            "command": "rclone sync --fast-list --bwlimit 8M /media/Musiikki secret:/Musiikki",
            "tokens": [
                "rclone",
                "sync",
                "--fast-list",
                "--bwlimit",
                "8M",
                "/media/Musiikki",
                "secret:/Musiikki",
            ],
        }
    ]


def test_step_run_buttons_do_not_create_nested_forms():
    job = JobRecord(id=1, name="backup", cron="", common_args="", env_json="{}")
    command_previews = [
        {
            "id": 2,
            "name": "Music",
            "command": "rclone sync /media/Musiikki secret:/Musiikki",
            "tokens": ["rclone", "sync", "/media/Musiikki", "secret:/Musiikki"],
        }
    ]

    html = templates.get_template("_job_editor.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="Music|sync /media/Musiikki secret:/Musiikki",
        command_previews=command_previews,
    )

    parser = NestedFormParser()
    parser.feed(html)

    assert not parser.has_nested_form
    assert 'form="run-step-2"' in html
    assert 'form="dry-run-step-2"' in html
    assert 'id="run-step-2" method="post" action="/jobs/1/steps/2/run"' in html
    assert 'id="dry-run-step-2" method="post" action="/jobs/1/steps/2/dry-run"' in html


def test_job_editor_ctrl_s_saves_without_navigating():
    html = templates.get_template("_job_editor.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True, common_args=""),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
    )

    assert '<form id="job-editor-form"' in html
    assert 'event.key.toLowerCase() !== "s"' in html
    assert "event.ctrlKey || event.metaKey" in html
    assert "fetch(jobForm.action" in html
    assert "new FormData(jobForm)" in html
    assert "response.text()" in html
    assert "new DOMParser()" in html
    assert "refreshSavedPage(html)" in html
    assert 'id="job-save-toast"' in html
    assert 'class="toast-icon"' in html
    assert 'toast.querySelector(".toast-icon")' in html
    assert 'showToast("Saved")' in html
    assert 'showToast("Save failed", "error")' in html
    assert 'replaceFromSavedPage(savedDocument, ".page-header")' in html
    assert 'replaceFromSavedPage(savedDocument, "#job-editor-form .command-preview")' in html
    assert "activeElement.focus()" in html
    assert "window.location.assign(response.url)" not in html
    assert 'data-background-save="true"' in html


def test_console_ctrl_v_pastes_clipboard_into_terminal():
    html = templates.get_template("console.html").render(recent=[], recent_commands=[])

    assert "navigator.clipboard.readText()" in html
    assert 'if (key === "v")' in html
    assert "insertPromptText(text);" in html
    assert "sendInput(text);" in html


def test_console_terminal_renders_ansi_sequences():
    html = templates.get_template("console.html").render(recent=[], recent_commands=[])
    js = Path("app/static/ansi.js").read_text()
    css = Path("app/static/styles.css").read_text()

    assert '<script src="/static/ansi.js"></script>' in html
    assert "rcloneRunnerAnsi.render" in html
    assert "ansi-red" in js
    assert ".ansi-red" in css


def test_log_viewer_renders_ansi_sequences():
    html = templates.get_template("_log_viewer.html").render(
        log_chunk=SimpleNamespace(
            text="\x1b[91mred\x1b[0m",
            next_before="",
            has_more=False,
            end_offset=12,
        ),
        log_chunk_url="/logs/chunk",
        log_append_url="/logs/append",
        status_url=None,
        raw_log_url="/logs/raw",
        run=SimpleNamespace(status="success"),
        log_status_url=None,
        log_append_offset=12,
    )

    assert '<script src="/static/ansi.js"></script>' in html
    assert "rcloneRunnerAnsi.render" in html


def test_history_tables_use_styled_status_labels():
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="canceled",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=1,
        step_name="Music",
        status="canceled",
        exit_code=None,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=2,
        command="lsd remote:",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 1, tzinfo=UTC),
    )

    history_html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[step_run],
        console_runs=[console_run],
    )
    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[job_run],
        runs=[step_run],
    )

    assert '<span class="run-status canceled">Canceled</span>' in history_html
    assert '<span class="run-status success">Success</span>' in history_html
    assert '<span class="run-status canceled">Canceled</span>' in job_html


def test_history_pages_link_to_whole_job_runs():
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    history_html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[],
        console_runs=[],
    )
    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[job_run],
        runs=[],
    )

    assert "<h2>Job runs</h2>" in history_html
    assert '<a href="/job-runs/3">Open</a>' in history_html
    assert '<h2 class="section-heading">Recent job runs</h2>' in job_html
    assert '<a href="/job-runs/3">Open</a>' in job_html


def test_history_tables_render_pagination_controls():
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[],
        console_runs=[],
        job_pagination={
            "page": 2,
            "has_previous": True,
            "has_next": True,
            "total_pages": 4,
            "previous_url": "/runs?job_page=1&step_page=3&console_page=4#job-runs-section",
            "next_url": "/runs?job_page=3&step_page=3&console_page=4#job-runs-section",
            "target": "job-runs-section",
        },
        step_pagination={"page": 3, "has_previous": False, "has_next": False},
        console_pagination={"page": 4, "has_previous": False, "has_next": False},
    )

    assert '<section class="history-section" id="job-runs-section">' in html
    assert '<nav class="pagination" aria-label="Job runs pagination">' in html
    assert 'data-history-target="job-runs-section"' in html
    assert 'href="/runs?job_page=1&amp;step_page=3&amp;console_page=4#job-runs-section"' in html
    assert 'href="/runs?job_page=3&amp;step_page=3&amp;console_page=4#job-runs-section"' in html
    assert "Page 2/4" in html
    assert "fetch(url)" in html
    assert "history.pushState" in html


def test_history_tables_render_individual_delete_actions():
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=4,
        step_name="Music",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=5,
        command="version",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[step_run],
        console_runs=[console_run],
    )

    assert 'action="/job-runs/3/delete"' in html
    assert 'action="/runs/4/delete"' in html
    assert 'action="/console/runs/5/delete"' in html
    assert "Delete" in html


def test_history_delete_actions_use_aligned_action_cells():
    css = Path("app/static/styles.css").read_text()
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=4,
        step_name="Music",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=5,
        command="version",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[step_run],
        console_runs=[console_run],
    )

    assert "vertical-align: middle" in css
    assert ".action-cell" in css
    assert '<th class="action-column"></th>' in html
    assert '<td class="action-cell">' in html


def test_job_history_tables_format_times_in_configured_timezone():
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 1, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=1,
        step_name="Music",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=2,
        command="lsd remote:",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    history_html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[step_run],
        console_runs=[console_run],
    )
    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[job_run],
        runs=[step_run],
    )

    assert "2026-04-26 21:00:00 EEST" in history_html
    assert "2026-04-26 21:01:00 EEST" in history_html
    assert "2026-04-26 18:00:00+00:00" not in history_html
    assert "2026-04-26 21:00:00 EEST" in job_html
    assert "2026-04-26 18:00:00+00:00" not in job_html


def test_job_summaries_show_human_schedule_without_raw_cron():
    job = SimpleNamespace(id=1, name="backup", cron="0 2 * * *", enabled=True, steps=[])

    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Daily at 02:00"}],
    )
    job_html = templates.get_template("job_detail.html").render(
        job=job,
        schedule_summary="Daily at 02:00",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[],
        runs=[],
    )

    assert "Daily at 02:00" in jobs_html
    assert "0 2 * * *" not in jobs_html
    assert "<p>Daily at 02:00 · Enabled</p>" in job_html


def test_jobs_page_renders_compact_run_context_column():
    css = Path("app/static/styles.css").read_text()
    job = SimpleNamespace(id=1, name="backup", cron="0 2 * * *", enabled=True, steps=[])
    disabled_job = SimpleNamespace(id=2, name="archive", cron="0 3 * * *", enabled=False, steps=[])
    unscheduled_job = SimpleNamespace(id=3, name="manual", cron="", enabled=True, steps=[])
    last_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 1, tzinfo=UTC),
    )

    html = templates.get_template("jobs.html").render(
        jobs=[
            {
                "record": job,
                "schedule_summary": "Daily at 02:00",
                "last_run": last_run,
                "next_run": datetime(2026, 4, 27, 23, 0, tzinfo=UTC),
            },
            {
                "record": disabled_job,
                "schedule_summary": "Daily at 03:00",
                "last_run": None,
                "next_run": None,
            },
            {
                "record": unscheduled_job,
                "schedule_summary": "Never",
                "last_run": None,
                "next_run": None,
            },
        ],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
    )

    assert "<th>Runs</th>" in html
    assert "<th>Last run</th>" not in html
    assert "<th>Next run</th>" not in html
    assert '<span class="run-status success">Success</span>' in html
    assert "<span>Last</span>" in html
    assert "<span>Next</span>" in html
    assert "2026-04-26 21:00:00 EEST" in html
    assert "2026-04-28 02:00:00 EEST" in html
    assert "No runs" in html
    assert "Disabled" in html
    assert "Never" in html
    assert "job-run-context" in css
    assert "job-actions" in css


def test_settings_page_shows_prune_feedback_and_clear_history_action():
    html = templates.get_template("settings.html").render(
        settings=SimpleNamespace(
            data_dir="/data",
            log_dir="/logs",
            timezone="Europe/Helsinki",
            retention_days=30,
        ),
        known_logs=3,
        pruned=2,
        cleared=4,
        email_settings=SimpleNamespace(
            enabled=True,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_username="user@gmail.com",
            smtp_password="secret",
            sender="user@gmail.com",
            recipients="ops@example.com",
            use_starttls=True,
            notify_success=False,
            notify_failure=True,
            notify_canceled=True,
            app_base_url="http://runner.local",
            include_log_tail_lines=200,
        ),
        email_saved=None,
        email_test=None,
        email_error=None,
    )

    assert "Deleted 2 old log files." in html
    assert "Cleared 4 history records and their known log files." in html
    assert 'action="/settings/clear-history"' in html
    assert "Clear all history" in html
    assert "Job configuration remains intact." in html
    assert 'action="/settings/email"' in html
    assert 'action="/settings/email/test"' in html
    assert 'name="smtp_password" value=""' in html
    assert "secret" not in html
    assert "Email notifications" in html
    assert "Maintenance" in html
    assert "Remove old log files according to the configured retention window." in html


def test_post_forms_include_csrf_fields_and_destructive_confirmations():
    csrf_input = '<input type="hidden" name="csrf_token" value="token">'
    job = SimpleNamespace(id=1, name="backup", cron="", enabled=True, steps=[])
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status="success",
        started_at=datetime(2026, 4, 26, 17, 59, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=4,
        step_name="Music",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=5,
        command="version",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )

    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": None}],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
        csrf_field=lambda: csrf_input,
    )
    settings_html = templates.get_template("settings.html").render(
        settings=SimpleNamespace(
            data_dir="/data", log_dir="/data/logs", timezone="UTC", retention_days=30
        ),
        known_logs=0,
        pruned=None,
        cleared=None,
        email_settings=SimpleNamespace(
            enabled=False,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            sender="",
            recipients="",
            use_starttls=True,
            notify_success=False,
            notify_failure=True,
            notify_canceled=True,
            app_base_url="",
            include_log_tail_lines=200,
        ),
        email_saved=None,
        email_test=None,
        email_error=None,
        csrf_field=lambda: csrf_input,
    )
    runs_html = templates.get_template("runs.html").render(
        job_runs=[job_run],
        step_runs=[step_run],
        console_runs=[console_run],
        csrf_field=lambda: csrf_input,
    )

    assert jobs_html.count('name="csrf_token"') == 4
    assert settings_html.count('name="csrf_token"') == 4
    assert runs_html.count('name="csrf_token"') == 4
    assert 'return confirm("Delete backup? Run history will be kept.")' in jobs_html
    assert "return confirm('Prune old log files?')" in settings_html
    assert "return confirm('Clear all run and console history?')" in settings_html
    assert "return confirm('Delete this run and its log file?')" in runs_html


def test_job_delete_actions_render_in_list_and_detail():
    csrf_input = '<input type="hidden" name="csrf_token" value="token">'
    job = SimpleNamespace(id=7, name="Nightly backup", cron="", enabled=True, steps=[])

    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": None}],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
        deleted=True,
        csrf_field=lambda: csrf_input,
    )
    detail_html = templates.get_template("job_detail.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[],
        runs=[],
        delete_blocked=True,
        csrf_field=lambda: csrf_input,
    )

    assert "Deleted job configuration. Run history was kept." in jobs_html
    assert 'method="post" action="/jobs/7/delete"' in jobs_html
    assert 'method="post" action="/jobs/7/delete"' in detail_html
    assert detail_html.count('name="csrf_token"') == 5
    assert "Delete job" in detail_html
    assert 'return confirm("Delete Nightly backup? Run history will be kept.")' in jobs_html
    assert 'return confirm("Delete Nightly backup? Run history will be kept.")' in detail_html
    assert "This job is running. It must finish or be canceled before deletion." in detail_html


class NestedFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_depth = 0
        self.has_nested_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            if self.form_depth:
                self.has_nested_form = True
            self.form_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form_depth -= 1

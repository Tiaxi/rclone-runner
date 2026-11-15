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
    assert ".tabs a.current" in css
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


def test_readme_includes_secure_setup_and_backup_guidance():
    readme = Path("README.md").read_text()

    assert "RCLONE_RUNNER_ADMIN_PASSWORD_HASH" in readme
    assert "RCLONE_RUNNER_SECRET_KEY" in readme
    assert "Back up" in readme
    assert "rclone.conf" in readme


def _running_row(**overrides):
    row = {
        "kind": "job",
        "name": "backup",
        "detail": "Step: Music",
        "mode": "Manual",
        "dry_run": False,
        "started_at": datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        "elapsed_seconds": 75.0,
        "progress": "1.2 GiB / 4 GiB, 30%, 12 MiB/s, ETA 4m",
        "url": "/job-runs/3",
    }
    row.update(overrides)
    return row


def test_jobs_page_lists_running_jobs_and_console_commands_together():
    html = templates.get_template("jobs.html").render(
        jobs=[],
        running_now=[
            _running_row(),
            _running_row(
                kind="console",
                name="lsd remote:",
                detail="Console command",
                mode="Console",
                elapsed_seconds=4.25,
                progress=None,
                url="/console/runs/5",
            ),
        ],
    )

    assert '<h2 id="running-now-heading">Running now</h2>' in html
    assert '<a href="/job-runs/3">backup</a>' in html
    assert "Step: Music" in html
    assert "1.2 GiB / 4 GiB, 30%, 12 MiB/s, ETA 4m" in html
    assert '<a href="/console/runs/5">Log</a>' in html
    assert "<code>lsd remote:</code>" in html
    assert 'data-elapsed-seconds="75.0">1m 15s</td>' in html
    assert 'data-elapsed-seconds="4.2">4.2s</td>' in html
    assert "2026-04-26 21:00:00 EEST" in html
    assert 'data-refresh-seconds="5"' in html
    assert html.index("Running now") < html.index("Configured jobs")


def test_jobs_page_hides_running_now_when_idle():
    html = templates.get_template("jobs.html").render(jobs=[], running_now=[])

    assert "Running now" not in html
    assert "currently active" not in html
    assert '<h2 id="configured-jobs-heading" class="visually-hidden">Configured jobs</h2>' in html
    assert 'data-refresh-seconds="30"' in html
    assert '<a href="/jobs/new">Create one</a>' in html


def test_jobs_page_refreshes_live_region_and_ticks_elapsed_times():
    html = templates.get_template("jobs.html").render(jobs=[], running_now=[])

    assert '<div id="jobs-live"' in html
    assert 'getElementById("jobs-live")' in html
    assert "replaceWith(next)" in html
    assert "visibilitychange" in html
    assert "[data-elapsed-seconds]" in html


def test_job_detail_history_heading_has_explicit_section_spacing():
    html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_run_rows=[],
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
            "deletes": None,
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
    assert "insertPastedText(text);" in html
    assert 'terminal.addEventListener("paste"' in html
    assert "event.clipboardData.getData" in html
    assert "if (navigator.clipboard?.readText)" in html
    assert 'if (key === "v")' in html
    assert "insertPromptText(text);" in html
    assert "insertRunningInputText(text);" in html


def test_console_running_command_uses_local_line_editor():
    html = templates.get_template("console.html").render(recent=[], recent_commands=[])

    assert "runningInputBuffer" in html
    assert "runningCursorIndex" in html
    assert "sendRunningInputLine()" in html
    assert "sendInput(`${runningInputBuffer}\\r`);" in html
    assert "moveRunningCursor(-1);" in html
    assert "moveRunningCursor(1);" in html
    assert "insertRunningInputText(event.key);" in html
    assert "committedText += `${runningInputBuffer}" not in html
    assert "sendInput(arrows[event.key]);" not in html


def test_console_caches_rendered_output_and_limits_scrollback():
    html = templates.get_template("console.html").render(recent=[], recent_commands=[])

    assert "const MAX_SCROLLBACK_CHARS = 300000;" in html
    assert "committedHtml = rcloneRunnerAnsi.render(committedText);" in html
    assert "rcloneRunnerAnsi.render(committedText)}" not in html
    assert html.count("committedText +=") == 1
    assert 'localStorage.getItem("terminalFontSize")' in html.split("try {", 1)[1]


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


def _history_html(view, runs, *, transfer=None, pagination=None, **extra):
    rows = [{"run": run, "duration": "1m 0s", "transfer": transfer} for run in runs]
    tabs = [
        {"view": name, "label": label, "count": 1, "url": url, "current": name == view}
        for name, label, url in (
            ("jobs", "Job runs", "/runs"),
            ("steps", "Step runs", "/runs?view=steps"),
            ("console", "Console", "/runs?view=console"),
        )
    ]
    default_pagination = {
        "page": 1,
        "total_pages": 1,
        "total_items": len(rows),
        "first_item": 1 if rows else 0,
        "last_item": len(rows),
        "has_previous": False,
        "has_next": False,
        "previous_url": None,
        "next_url": None,
    }
    return templates.get_template("runs.html").render(
        view=view, tabs=tabs, rows=rows, pagination=pagination or default_pagination, **extra
    )


def _history_fixtures(job_status="success", step_status="success", console_status="success"):
    job_run = SimpleNamespace(
        id=3,
        job_name="backup",
        trigger="manual",
        status=job_status,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 26, 18, 1, tzinfo=UTC),
    )
    step_run = SimpleNamespace(
        id=4,
        step_name="Music",
        status=step_status,
        exit_code=0 if step_status == "success" else None,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        job_run=SimpleNamespace(job_name="backup", trigger="manual"),
    )
    console_run = SimpleNamespace(
        id=5,
        command="lsd remote:",
        status=console_status,
        exit_code=0,
        started_at=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
    )
    return job_run, step_run, console_run


def test_history_tables_use_styled_status_labels():
    job_run, step_run, console_run = _history_fixtures("canceled", "canceled", "success")

    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_run_rows=[{"run": job_run, "duration": "1m 0s", "transfer": None}],
        runs=[step_run],
    )

    assert '<span class="run-status canceled">Canceled</span>' in _history_html("jobs", [job_run])
    assert '<span class="run-status canceled">Canceled</span>' in _history_html("steps", [step_run])
    assert '<span class="run-status success">Success</span>' in _history_html(
        "console", [console_run]
    )
    assert '<span class="run-status canceled">Canceled</span>' in job_html


def test_history_rows_link_to_their_runs():
    job_run, step_run, console_run = _history_fixtures()

    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_run_rows=[{"run": job_run, "duration": "1m 0s", "transfer": None}],
        runs=[],
    )

    assert '<a href="/job-runs/3">backup</a>' in _history_html("jobs", [job_run])
    steps_html = _history_html("steps", [step_run])
    assert '<a href="/runs/4">Music</a>' in steps_html
    assert '<div class="table-subtext">backup</div>' in steps_html
    assert (
        '<a class="command-link" href="/console/runs/5"><code>lsd remote:</code></a>'
        in _history_html("console", [console_run])
    )
    assert '<h2 class="section-heading">Recent job runs</h2>' in job_html
    assert '<a href="/job-runs/3"><time datetime="2026-04-26T21:00:00+03:00"' in job_html


def test_history_renders_one_view_with_tabs_and_pagination():
    html = _history_html(
        "steps",
        [],
        pagination={
            "page": 2,
            "total_pages": 4,
            "total_items": 80,
            "first_item": 26,
            "last_item": 50,
            "has_previous": True,
            "has_next": True,
            "previous_url": "/runs?view=steps",
            "next_url": "/runs?view=steps&page=3",
        },
    )

    assert '<nav class="tabs" aria-label="History views">' in html
    assert '<a href="/runs?view=steps" class="current" aria-current="page">Step runs' in html
    assert '<a href="/runs">Job runs <span class="tab-count">1</span></a>' in html
    assert "<th>Step</th>" in html
    assert "<th>Job</th>" not in html
    assert "<th>Command</th>" not in html
    assert "26–50 of 80" in html
    assert '<a class="button secondary small" href="/runs?view=steps">Newer</a>' in html
    assert '<a class="button secondary small" href="/runs?view=steps&amp;page=3">Older</a>' in html


def test_history_shows_duration_and_transfer_summary():
    job_run, _, _ = _history_fixtures()

    html = _history_html("jobs", [job_run], transfer={"data": "1.500 GiB", "detail": "3 files"})

    assert "<th>Duration</th>" in html
    assert "<th>Transferred</th>" in html
    assert "1m 0s" in html
    assert "<div>1.500 GiB</div>" in html
    assert '<div class="table-subtext">3 files</div>' in html


def test_history_tables_render_individual_delete_actions():
    job_run, step_run, console_run = _history_fixtures()
    pagination = {
        "page": 2,
        "total_pages": 2,
        "total_items": 26,
        "first_item": 26,
        "last_item": 26,
        "has_previous": True,
        "has_next": False,
        "previous_url": "/runs",
        "next_url": None,
    }

    jobs_html = _history_html("jobs", [job_run], pagination=pagination)

    assert 'action="/job-runs/3/delete"' in jobs_html
    assert '<input type="hidden" name="page" value="2">' in jobs_html
    assert '<button class="danger quiet small" type="submit">Delete</button>' in jobs_html
    assert 'action="/runs/4/delete"' in _history_html("steps", [step_run])
    assert 'action="/console/runs/5/delete"' in _history_html("console", [console_run])


def test_history_delete_actions_use_aligned_action_cells():
    css = Path("app/static/styles.css").read_text()
    job_run, step_run, console_run = _history_fixtures()

    for html in (
        _history_html("jobs", [job_run]),
        _history_html("steps", [step_run]),
        _history_html("console", [console_run]),
    ):
        assert '<th class="action-column"></th>' in html
        assert '<td class="action-cell">' in html
    assert "vertical-align: middle" in css
    assert ".action-cell" in css


def test_job_history_tables_format_times_in_configured_timezone():
    job_run, step_run, console_run = _history_fixtures()

    job_html = templates.get_template("job_detail.html").render(
        job=SimpleNamespace(id=1, name="backup", cron="", enabled=True),
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_run_rows=[{"run": job_run, "duration": "1m 0s", "transfer": None}],
        runs=[step_run],
    )

    for history_html in (
        _history_html("jobs", [job_run]),
        _history_html("steps", [step_run]),
        _history_html("console", [console_run]),
    ):
        assert "2026-04-26 21:00:00 EEST" in history_html
        assert "2026-04-26 18:00:00+00:00" not in history_html
    # Tables show minutes and keep the full local time in a tooltip.
    assert 'title="2026-04-26 21:00:00 EEST">2026-04-26 21:00</time>' in job_html
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
        job_run_rows=[],
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
        running_now=[],
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
    job_run, _, _ = _history_fixtures()

    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": None}],
        running_now=[],
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
    runs_html = _history_html("jobs", [job_run], csrf_field=lambda: csrf_input)

    assert jobs_html.count('name="csrf_token"') == 4
    # Log out, email settings, expected speed, prune, and clear history.
    assert settings_html.count('name="csrf_token"') == 5
    # Logout and the run's delete form.
    assert runs_html.count('name="csrf_token"') == 2
    assert 'return confirm("Delete backup? Run history will be kept.")' in jobs_html
    assert "return confirm('Prune old log files?')" in settings_html
    assert "return confirm('Clear all run and console history?')" in settings_html
    assert "return confirm('Delete this run and its log file?')" in runs_html


def test_job_delete_actions_render_in_list_and_detail():
    csrf_input = '<input type="hidden" name="csrf_token" value="token">'
    job = SimpleNamespace(id=7, name="Nightly backup", cron="", enabled=True, steps=[])

    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": None}],
        running_now=[],
        deleted=True,
        csrf_field=lambda: csrf_input,
    )
    detail_html = templates.get_template("job_detail.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_run_rows=[],
        runs=[],
        delete_blocked=True,
        csrf_field=lambda: csrf_input,
    )

    assert "Deleted job configuration. Run history was kept." in jobs_html
    assert 'method="post" action="/jobs/7/delete"' in jobs_html
    assert 'method="post" action="/jobs/7/delete"' in detail_html
    # Logout, run, dry run, duplicate, delete, and the job editor.
    assert detail_html.count('name="csrf_token"') == 6
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


def test_real_runs_ask_for_confirmation_but_dry_runs_do_not():
    csrf_input = '<input type="hidden" name="csrf_token" value="token">'
    job = SimpleNamespace(id=1, name="Bob's backup", cron="", enabled=True, steps=[])
    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": None}],
        running_now=[],
        csrf_field=lambda: csrf_input,
    )
    detail_html = templates.get_template("job_detail.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[{"id": 7, "name": "Music", "command": "rclone sync a b", "tokens": []}],
        job_run_rows=[],
        runs=[],
        delete_blocked=False,
        csrf_field=lambda: csrf_input,
    )

    for html in (jobs_html, detail_html):
        run_form = html.split('action="/jobs/1/run"', 1)[1].split(">", 1)[0]
        dry_run_form = html.split('action="/jobs/1/dry-run"', 1)[1].split(">", 1)[0]
        assert "return confirm(" in run_form
        assert "Bob\\u0027s backup now? This is a real run" in run_form
        assert "confirm(" not in dry_run_form
    step_form = detail_html.split('action="/jobs/1/steps/7/run"', 1)[1].split(">", 1)[0]
    step_dry_run_form = detail_html.split('action="/jobs/1/steps/7/dry-run"', 1)[1].split(">", 1)[0]
    assert "Run step Music now?" in step_form
    assert "confirm(" not in step_dry_run_form


def test_hidden_attribute_wins_over_component_display_rules():
    css = Path("app/static/styles.css").read_text()

    assert "[hidden] {\n  display: none !important;\n}" in css


def test_history_hides_delete_for_runs_still_in_progress():
    job_run, step_run, console_run = _history_fixtures("running", "success", "running")
    step_run.job_run = SimpleNamespace(job_name="backup", trigger="manual", status="running")

    for view, run, action in (
        ("jobs", job_run, "/job-runs/3/delete"),
        ("steps", step_run, "/runs/4/delete"),
        ("console", console_run, "/console/runs/5/delete"),
    ):
        assert action not in _history_html(view, [run])


def test_step_log_reveals_delete_from_the_servers_can_delete_flag():
    viewer = Path("app/templates/_log_viewer.html").read_text()

    assert "revealDeleteWhenAllowed();" in viewer
    assert ".can_delete" in viewer


def test_command_preview_fills_in_run_variables():
    import re

    job = JobRecord(
        name="backup",
        cron="",
        common_args="--backup-dir crypt:versions/${RUN_DATE}",
        env_json="{}",
    )
    job.steps = [JobStepRecord(position=1, name="Musa", command="sync /media/Musa crypt:Musa")]

    (preview,) = _command_previews(job)

    assert re.search(r"crypt:versions/\d{4}-\d{2}-\d{2}( |$)", preview["command"])

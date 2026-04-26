import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from app.db import JobRecord, JobStepRecord
from app.main import _command_previews, templates


def test_headings_after_large_blocks_have_section_spacing():
    css = Path("app/static/styles.css").read_text()

    assert ".table-wrap + h2" in css
    assert ".form-grid + h2" in css
    assert ".terminal-output + h2" in css
    assert ".section-heading" in css


def test_base_template_links_svg_favicon():
    html = templates.get_template("base.html").render()

    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in html
    assert '<img class="brand-mark" src="/static/favicon.svg" alt="" aria-hidden="true">' in html


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
            "previous_url": "/runs?job_page=1&step_page=3&console_page=4",
            "next_url": "/runs?job_page=3&step_page=3&console_page=4",
        },
        step_pagination={"page": 3, "has_previous": False, "has_next": False},
        console_pagination={"page": 4, "has_previous": False, "has_next": False},
    )

    assert '<nav class="pagination" aria-label="Job runs pagination">' in html
    assert 'href="/runs?job_page=1&amp;step_page=3&amp;console_page=4"' in html
    assert 'href="/runs?job_page=3&amp;step_page=3&amp;console_page=4"' in html
    assert "Page 2" in html


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

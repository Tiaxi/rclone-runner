# Deployment Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the remaining deployment-readiness features for public repo publication: safety warnings, CSRF, health checks, destructive confirmations, job deletion, jobs-page run context, and deployment docs.

**Architecture:** Keep the current FastAPI/Jinja/SQLite architecture. Add small helpers in `app/auth.py`, `app/config.py`, `app/main.py`, and `app/scheduler.py`; update existing templates rather than introducing a frontend framework. Each task is independently testable and committable.

**Tech Stack:** FastAPI, Starlette sessions, Jinja2 templates, SQLAlchemy, APScheduler, pytest, Docker Compose.

---

## File Map

- Modify `app/config.py`: add constants/helpers for insecure runtime configuration warnings.
- Modify `app/auth.py`: add session-backed CSRF token helpers and validation dependency.
- Modify `app/main.py`: add `/health`, wire CSRF validation into POST routes, add job deletion, pass runtime warnings and jobs-page visibility data.
- Modify `app/scheduler.py`: add a helper to compute the next scheduled fire time from a cron expression.
- Modify `app/templates/base.html`: render runtime warning banner and support shared CSRF hidden field.
- Modify `app/templates/login.html`: include CSRF token in the login form.
- Modify `app/templates/jobs.html`: show last run, next run, delete actions, and CSRF fields.
- Modify `app/templates/job_detail.html`: show job deletion and notice messages.
- Modify `app/templates/_job_editor.html`: include CSRF token in editor and per-step forms.
- Modify `app/templates/runs.html`, `app/templates/run_detail.html`, `app/templates/job_run_detail.html`, `app/templates/console_run_detail.html`, `app/templates/settings.html`, `app/templates/base.html`: include CSRF fields and destructive confirmations in existing POST forms.
- Modify `docker-compose.yml`: add a healthcheck hitting `/health`.
- Modify `README.md`: add public-repo deployment checklist and backup guidance.
- Modify tests in `tests/test_auth.py`, `tests/test_web.py`, and `tests/test_job_preview.py`; add focused tests only where needed.

---

### Task 1: Health Endpoint And Deployment Docs

**Files:**
- Modify: `app/main.py`
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Test: `tests/test_web.py`
- Test: `tests/test_job_preview.py`

- [ ] **Step 1: Write failing health endpoint test**

Add to `tests/test_web.py`:

```python
async def test_health_endpoint_is_public_and_reports_ok():
    response = await main.health()

    assert response == {"status": "ok"}
```

- [ ] **Step 2: Run the focused health test and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_health_endpoint_is_public_and_reports_ok
```

Expected: fail because `main.health` does not exist.

- [ ] **Step 3: Implement `/health`**

Add near the root route in `app/main.py`:

```python
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 4: Run the focused health test and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_health_endpoint_is_public_and_reports_ok
```

Expected: pass.

- [ ] **Step 5: Write failing Docker and README tests**

Add to `tests/test_job_preview.py`:

```python
def test_compose_declares_healthcheck():
    compose = Path("docker-compose.yml").read_text()

    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8000/health" in compose


def test_readme_includes_deployment_checklist_and_backup_guidance():
    readme = Path("README.md").read_text()

    assert "## Deployment Checklist" in readme
    assert "RCLONE_RUNNER_ADMIN_PASSWORD_HASH" in readme
    assert "RCLONE_RUNNER_SECRET_KEY" in readme
    assert "/health" in readme
    assert "Back up" in readme
    assert "rclone.conf" in readme
```

- [ ] **Step 6: Run focused docs tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_job_preview.py::test_compose_declares_healthcheck tests/test_job_preview.py::test_readme_includes_deployment_checklist_and_backup_guidance
```

Expected: fail because the compose healthcheck and checklist are absent.

- [ ] **Step 7: Add Docker healthcheck and README checklist**

In `docker-compose.yml`, add under the service:

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
```

In `README.md`, add `## Deployment Checklist` before Security Notes:

```markdown
## Deployment Checklist

Before publishing the app on a network:

- Set `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` to a generated password hash.
- Set `RCLONE_RUNNER_SECRET_KEY` to a long random value.
- Configure persistent mounts for `/data`, `/config/rclone`, and `/media`.
- Set `RCLONE_RUNNER_TIMEZONE` and `RCLONE_RUNNER_RETENTION_DAYS`.
- Verify `GET /health` returns `{"status":"ok"}` after startup.
- Use HTTPS or a trusted reverse proxy if the app is exposed beyond a LAN.
- Back up `/data` and `rclone.conf`; these contain the database, logs, and rclone remote configuration.

Rclone Runner is designed for a trusted single-admin environment, not as a public multi-user service.
```

- [ ] **Step 8: Verify task tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_health_endpoint_is_public_and_reports_ok tests/test_job_preview.py::test_compose_declares_healthcheck tests/test_job_preview.py::test_readme_includes_deployment_checklist_and_backup_guidance
```

Expected: pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add app/main.py docker-compose.yml README.md tests/test_web.py tests/test_job_preview.py
git commit -m "feat: add deployment health check"
```

---

### Task 2: Runtime Safety Warnings

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_auth.py`
- Test: `tests/test_job_preview.py`

- [ ] **Step 1: Write failing config warning helper tests**

Add to `tests/test_auth.py`:

```python
from app.config import Settings, runtime_warnings


def test_runtime_warnings_flag_missing_password_hash_and_default_secret():
    warnings = runtime_warnings(Settings(admin_password_hash="", secret_key="change-me"))

    assert "development admin password" in warnings[0]
    assert "default session secret" in warnings[1]


def test_runtime_warnings_are_empty_for_hardened_config():
    warnings = runtime_warnings(Settings(admin_password_hash="hash", secret_key="not-default"))

    assert warnings == []
```

- [ ] **Step 2: Run helper tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py::test_runtime_warnings_flag_missing_password_hash_and_default_secret tests/test_auth.py::test_runtime_warnings_are_empty_for_hardened_config
```

Expected: import failure for `runtime_warnings`.

- [ ] **Step 3: Implement config warning helper**

In `app/config.py`, add:

```python
DEFAULT_SECRET_KEY = "change-me"


def runtime_warnings(value: Settings) -> list[str]:
    warnings: list[str] = []
    if not value.admin_password_hash:
        warnings.append(
            "RCLONE_RUNNER_ADMIN_PASSWORD_HASH is not set; the development admin password is enabled."
        )
    if value.secret_key in {DEFAULT_SECRET_KEY, "change-this-long-random-string"}:
        warnings.append(
            "RCLONE_RUNNER_SECRET_KEY uses a default session secret; set a long random value."
        )
    return warnings
```

Also set the default field to the constant:

```python
secret_key: str = DEFAULT_SECRET_KEY
```

- [ ] **Step 4: Run helper tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py::test_runtime_warnings_flag_missing_password_hash_and_default_secret tests/test_auth.py::test_runtime_warnings_are_empty_for_hardened_config
```

Expected: pass.

- [ ] **Step 5: Write failing template warning test**

Add to `tests/test_job_preview.py`:

```python
def test_base_template_renders_runtime_warnings():
    html = templates.get_template("base.html").render(
        runtime_warnings=["RCLONE_RUNNER_SECRET_KEY uses a default session secret."]
    )
    css = Path("app/static/styles.css").read_text()

    assert 'class="runtime-warning"' in html
    assert "RCLONE_RUNNER_SECRET_KEY uses a default session secret." in html
    assert ".runtime-warning" in css
```

- [ ] **Step 6: Run template warning test and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_job_preview.py::test_base_template_renders_runtime_warnings
```

Expected: fail because `base.html` does not render warnings.

- [ ] **Step 7: Render warnings globally**

In `app/main.py`, import the helper:

```python
from app.config import runtime_warnings, settings
```

Add a context processor after template globals:

```python
@templates.context_processor
def inject_runtime_warnings(request: Request) -> dict[str, object]:
    return {"runtime_warnings": runtime_warnings(settings)}
```

In `app/templates/base.html`, inside `<main class="page">` before `{% block content %}`:

```jinja2
      {% if runtime_warnings %}
      <section class="runtime-warning" role="alert">
        <strong>Deployment warning</strong>
        <ul>
          {% for warning in runtime_warnings %}
          <li>{{ warning }}</li>
          {% endfor %}
        </ul>
      </section>
      {% endif %}
```

In `app/static/styles.css`, add:

```css
.runtime-warning {
  background: var(--notice-bg);
  border: 1px solid var(--notice-border);
  border-radius: 8px;
  color: var(--notice-text);
  margin-bottom: 20px;
  padding: 12px 14px;
}

.runtime-warning ul {
  margin: 8px 0 0;
  padding-left: 20px;
}
```

- [ ] **Step 8: Verify task tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py::test_runtime_warnings_flag_missing_password_hash_and_default_secret tests/test_auth.py::test_runtime_warnings_are_empty_for_hardened_config tests/test_job_preview.py::test_base_template_renders_runtime_warnings
```

Expected: pass.

- [ ] **Step 9: Commit Task 2**

```bash
git add app/config.py app/main.py app/templates/base.html app/static/styles.css tests/test_auth.py tests/test_job_preview.py
git commit -m "feat: warn about insecure deployment config"
```

---

### Task 3: CSRF Protection And Destructive Confirmations

**Files:**
- Modify: `app/auth.py`
- Modify: `app/main.py`
- Modify: all templates containing POST forms
- Test: `tests/test_auth.py`
- Test: `tests/test_web.py`
- Test: `tests/test_job_preview.py`

- [ ] **Step 1: Write failing CSRF helper tests**

Add to `tests/test_auth.py`:

```python
from fastapi import HTTPException
from starlette.datastructures import FormData

from app.auth import CSRF_SESSION_KEY, csrf_field, require_csrf


class SessionRequest:
    def __init__(self):
        self.session = {}


async def test_csrf_field_creates_stable_session_token():
    request = SessionRequest()

    first = csrf_field(request)
    second = csrf_field(request)

    assert first == second
    assert f'name="csrf_token" value="{request.session[CSRF_SESSION_KEY]}"' in first


async def test_require_csrf_rejects_invalid_token():
    request = SessionRequest()
    request.session[CSRF_SESSION_KEY] = "expected"

    try:
        await require_csrf(request, csrf_token="wrong")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("invalid CSRF token was accepted")
```

- [ ] **Step 2: Run CSRF helper tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py::test_csrf_field_creates_stable_session_token tests/test_auth.py::test_require_csrf_rejects_invalid_token
```

Expected: import failure for CSRF helpers.

- [ ] **Step 3: Implement CSRF helpers**

In `app/auth.py`, add:

```python
CSRF_SESSION_KEY = "csrf_token"


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_field(request: Request) -> str:
    token = csrf_token(request)
    return f'<input type="hidden" name="csrf_token" value="{token}">'


async def require_csrf(request: Request, csrf_token: str = Form("")) -> None:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not hmac.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
```

Also import `Form` from FastAPI:

```python
from fastapi import Depends, Form, HTTPException, Request, status
```

- [ ] **Step 4: Run CSRF helper tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py::test_csrf_field_creates_stable_session_token tests/test_auth.py::test_require_csrf_rejects_invalid_token
```

Expected: pass.

- [ ] **Step 5: Write failing route and template tests**

Add to `tests/test_web.py`:

```python
async def test_login_page_includes_csrf_field():
    request = Request({"type": "http", "method": "GET", "path": "/login", "headers": [], "session": {}})

    response = await login_form(request)
    html = response.body.decode()

    assert 'name="csrf_token"' in html


async def test_create_job_rejects_missing_csrf():
    request = Request({"type": "http", "method": "POST", "path": "/jobs", "headers": [], "session": {}})

    with pytest.raises(Exception) as exc_info:
        await main.require_csrf(request, csrf_token="")

    assert getattr(exc_info.value, "status_code", None) == 403
```

Add or update in `tests/test_job_preview.py`:

```python
def test_post_forms_include_csrf_fields_and_destructive_confirmations():
    jobs_html = templates.get_template("jobs.html").render(
        jobs=[],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
        csrf_field=lambda: '<input type="hidden" name="csrf_token" value="token">',
    )
    settings_html = templates.get_template("settings.html").render(
        settings=SimpleNamespace(data_dir="/data", log_dir="/data/logs", timezone="UTC", retention_days=30),
        known_logs=0,
        pruned=None,
        cleared=None,
        csrf_field=lambda: '<input type="hidden" name="csrf_token" value="token">',
    )

    assert 'name="csrf_token"' in jobs_html
    assert 'name="csrf_token"' in settings_html
    assert 'onsubmit="return confirm(' in settings_html
```

- [ ] **Step 6: Run route/template tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_login_page_includes_csrf_field tests/test_web.py::test_create_job_rejects_missing_csrf tests/test_job_preview.py::test_post_forms_include_csrf_fields_and_destructive_confirmations
```

Expected: fail because forms and route imports are not wired.

- [ ] **Step 7: Wire CSRF into templates**

In `app/main.py`, import:

```python
from app.auth import SESSION_KEY, AuthRequired, csrf_field, require_csrf, verify_password
```

Add to the context processor:

```python
"csrf_field": lambda: csrf_field(request),
```

Add `{{ csrf_field()|safe }}` inside every POST form:

- `app/templates/base.html` logout form.
- `app/templates/login.html` login form.
- `app/templates/jobs.html` run and dry-run forms.
- `app/templates/job_detail.html` run and dry-run forms.
- `app/templates/_job_editor.html` editor form and per-step forms.
- `app/templates/runs.html` delete forms.
- `app/templates/run_detail.html` cancel and delete forms.
- `app/templates/job_run_detail.html` cancel and delete forms.
- `app/templates/console_run_detail.html` delete form.
- `app/templates/settings.html` prune and clear-history forms.

Add destructive confirmations:

```jinja2
onsubmit="return confirm('Delete this run and its log file?')"
onsubmit="return confirm('Clear all run and console history?')"
onsubmit="return confirm('Prune old log files?')"
```

Use object-specific text where the template has a name, for example job deletion in Task 4.

- [ ] **Step 8: Wire CSRF dependency into POST handlers**

In `app/main.py`, add a parameter to every POST route:

```python
_csrf: Annotated[None, Depends(require_csrf)],
```

Import `Annotated` and `Depends`:

```python
from typing import Annotated
from fastapi import Depends, FastAPI, Form, Request, WebSocket
```

Apply to login, logout, create/update job, run/dry-run routes, cancel/delete routes, console delete, prune, and clear history.

- [ ] **Step 9: Verify CSRF task tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_auth.py tests/test_web.py::test_login_page_includes_csrf_field tests/test_web.py::test_create_job_rejects_missing_csrf tests/test_job_preview.py::test_post_forms_include_csrf_fields_and_destructive_confirmations
```

Expected: pass.

- [ ] **Step 10: Run full suite to catch route signature fallout**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest
```

Expected: pass. If direct route-call tests fail because they do not pass `_csrf`, update those tests to pass `None` for the new dependency parameter.

- [ ] **Step 11: Commit Task 3**

```bash
git add app/auth.py app/main.py app/templates app/static/styles.css tests/test_auth.py tests/test_web.py tests/test_job_preview.py
git commit -m "feat: protect form posts with csrf"
```

---

### Task 4: Job Deletion

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/jobs.html`
- Modify: `app/templates/job_detail.html`
- Test: `tests/test_web.py`
- Test: `tests/test_job_preview.py`

- [ ] **Step 1: Write failing job deletion tests**

Add to `tests/test_web.py`:

```python
async def test_delete_job_removes_configuration_but_keeps_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            db.add(
                JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger="manual",
                    status="success",
                    started_at=main.utc_now(),
                    ended_at=main.utc_now(),
                )
            )
            db.commit()

            response = await main.delete_job(job.id, None, db, None)

            assert response.status_code == 303
            assert response.headers["location"] == "/jobs?deleted=1"
            assert db.get(JobRecord, job.id) is None
            assert db.query(JobRunRecord).count() == 1


async def test_delete_job_is_blocked_while_running():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            db.add(
                JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger="manual",
                    status="running",
                    started_at=main.utc_now(),
                    ended_at=None,
                )
            )
            db.commit()

            response = await main.delete_job(job.id, None, db, None)

            assert response.status_code == 303
            assert response.headers["location"] == f"/jobs/{job.id}?delete_blocked=1"
            assert db.get(JobRecord, job.id) is not None
```

- [ ] **Step 2: Run deletion tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_delete_job_removes_configuration_but_keeps_history tests/test_web.py::test_delete_job_is_blocked_while_running
```

Expected: fail because `delete_job` does not exist.

- [ ] **Step 3: Implement job deletion route and helper**

In `app/main.py`, add `deleted` to `jobs` and `delete_blocked` to `job_detail` query params.

Add route:

```python
@app.post("/jobs/{job_id}/delete")
async def delete_job(
    job_id: int,
    _: AuthRequired,
    db: DbSession,
    _csrf: Annotated[None, Depends(require_csrf)],
) -> Response:
    job = db.get(JobRecord, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    has_running_run = (
        db.query(JobRunRecord).filter_by(job_id=job_id, status="running").first() is not None
    )
    if has_running_run:
        return RedirectResponse(f"/jobs/{job_id}?delete_blocked=1", status_code=303)
    db.delete(job)
    db.commit()
    sync_schedules(db)
    return RedirectResponse("/jobs?deleted=1", status_code=303)
```

In direct-call tests, if CSRF was not implemented yet in the branch order, omit `_csrf`; if Task 3 is complete, pass `None`.

- [ ] **Step 4: Run deletion tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_delete_job_removes_configuration_but_keeps_history tests/test_web.py::test_delete_job_is_blocked_while_running
```

Expected: pass.

- [ ] **Step 5: Write failing UI tests**

Add to `tests/test_job_preview.py`:

```python
def test_job_delete_actions_render_in_list_and_detail():
    job = SimpleNamespace(id=7, name="backup", cron="", enabled=True, common_args="", steps=[])
    jobs_html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": None, "next_run": "Never"}],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
        deleted=None,
        csrf_field=lambda: '<input type="hidden" name="csrf_token" value="token">',
    )
    detail_html = templates.get_template("job_detail.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="",
        command_previews=[],
        job_runs=[],
        runs=[],
        delete_blocked=None,
        csrf_field=lambda: '<input type="hidden" name="csrf_token" value="token">',
    )

    assert 'action="/jobs/7/delete"' in jobs_html
    assert 'action="/jobs/7/delete"' in detail_html
    assert "Delete job" in detail_html
    assert "Delete backup?" in detail_html
```

- [ ] **Step 6: Run UI test and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_job_preview.py::test_job_delete_actions_render_in_list_and_detail
```

Expected: fail because delete forms are absent.

- [ ] **Step 7: Add job delete UI**

In `app/templates/jobs.html`, add a delete form in the configured jobs action row:

```jinja2
<form method="post" action="/jobs/{{ job.id }}/delete" onsubmit="return confirm('Delete {{ job.name }}? History will be kept.')">
  {{ csrf_field()|safe }}
  <button class="danger" type="submit">Delete</button>
</form>
```

In `app/templates/job_detail.html`, add a delete form to the page-header button row:

```jinja2
<form method="post" action="/jobs/{{ job.id }}/delete" onsubmit="return confirm('Delete {{ job.name }}? History will be kept.')">
  {{ csrf_field()|safe }}
  <button class="danger" type="submit">Delete job</button>
</form>
```

Render notices:

```jinja2
{% if delete_blocked %}
<p class="notice">This job has a running job run. Cancel or wait for it to finish before deleting the job.</p>
{% endif %}
```

In `jobs.html`, render deleted notice:

```jinja2
{% if deleted %}
<p class="notice">Deleted job configuration. Run history was kept.</p>
{% endif %}
```

- [ ] **Step 8: Verify job deletion task**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_delete_job_removes_configuration_but_keeps_history tests/test_web.py::test_delete_job_is_blocked_while_running tests/test_job_preview.py::test_job_delete_actions_render_in_list_and_detail
```

Expected: pass.

- [ ] **Step 9: Commit Task 4**

```bash
git add app/main.py app/templates/jobs.html app/templates/job_detail.html tests/test_web.py tests/test_job_preview.py
git commit -m "feat: delete job configurations"
```

---

### Task 5: Jobs-Page Last And Next Run Context

**Files:**
- Modify: `app/scheduler.py`
- Modify: `app/main.py`
- Modify: `app/templates/jobs.html`
- Test: `tests/test_schedule.py`
- Test: `tests/test_web.py`
- Test: `tests/test_job_preview.py`

- [ ] **Step 1: Write failing next-run helper tests**

Add to `tests/test_schedule.py`:

```python
from app.scheduler import next_run_time


def test_next_run_time_returns_none_for_never_schedule():
    assert next_run_time("") is None


def test_next_run_time_returns_future_time_for_daily_schedule():
    value = next_run_time("0 2 * * *")

    assert value is not None
```

- [ ] **Step 2: Run next-run helper tests and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_schedule.py::test_next_run_time_returns_none_for_never_schedule tests/test_schedule.py::test_next_run_time_returns_future_time_for_daily_schedule
```

Expected: import failure for `next_run_time`.

- [ ] **Step 3: Implement next-run helper**

In `app/scheduler.py`, add:

```python
def next_run_time(expression: str):
    normalized = normalize_cron(expression)
    if not normalized:
        return None
    return cron_trigger(normalized).get_next_fire_time(None, datetime.now(ZoneInfo(settings.timezone)))
```

Import `datetime`:

```python
from datetime import datetime
```

- [ ] **Step 4: Run next-run helper tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_schedule.py::test_next_run_time_returns_none_for_never_schedule tests/test_schedule.py::test_next_run_time_returns_future_time_for_daily_schedule
```

Expected: pass.

- [ ] **Step 5: Write failing jobs-page context test**

Add to `tests/test_web.py`:

```python
async def test_jobs_page_includes_last_and_next_run_context():
    request = Request({"type": "http", "method": "GET", "path": "/jobs", "headers": []})
    with tempfile.TemporaryDirectory() as tmpdir:
        session_factory = _session_factory(Path(tmpdir) / "runs.db")
        with session_factory() as db:
            job = _create_job(db)
            db.add(
                JobRunRecord(
                    job_id=job.id,
                    job_name=job.name,
                    trigger="manual",
                    status="success",
                    started_at=main.utc_now(),
                    ended_at=main.utc_now(),
                )
            )
            db.commit()

            response = await main.jobs(request, None, db)
            row = response.context["jobs"][0]

            assert row["last_run"].status == "success"
            assert row["next_run"] == "Never"
```

- [ ] **Step 6: Run context test and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_jobs_page_includes_last_and_next_run_context
```

Expected: fail because `_job_rows` does not provide `last_run` or `next_run`.

- [ ] **Step 7: Implement job row context**

In `app/main.py`, import:

```python
from app.scheduler import next_run_time, scheduler, sync_schedules
```

Change `_job_rows` to accept `db`:

```python
def _job_rows(records: list[JobRecord], db) -> list[dict[str, object]]:
    rows = []
    for record in records:
        last_run = (
            db.query(JobRunRecord)
            .filter_by(job_id=record.id)
            .order_by(JobRunRecord.started_at.desc())
            .first()
        )
        next_run = None
        if record.enabled:
            next_run = next_run_time(record.cron)
        rows.append(
            {
                "record": record,
                "schedule_summary": cron_summary(record.cron),
                "last_run": last_run,
                "next_run": next_run,
            }
        )
    return rows
```

Update call site:

```python
"jobs": _job_rows(records, db),
```

- [ ] **Step 8: Run context test and verify GREEN**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py::test_jobs_page_includes_last_and_next_run_context
```

Expected: pass.

- [ ] **Step 9: Write failing jobs template test**

Update the existing jobs-page rendering tests or add:

```python
def test_jobs_page_renders_last_and_next_run_columns():
    now = datetime(2026, 4, 26, 18, 0, tzinfo=UTC)
    job = SimpleNamespace(id=1, name="backup", cron="", enabled=True, steps=[])
    last_run = SimpleNamespace(status="success", started_at=now, exit_code=0)

    html = templates.get_template("jobs.html").render(
        jobs=[{"record": job, "schedule_summary": "Never", "last_run": last_run, "next_run": None}],
        ongoing_job_runs=[],
        ongoing_step_runs=[],
        ongoing_console_runs=[],
        csrf_field=lambda: '<input type="hidden" name="csrf_token" value="token">',
    )

    assert "<th>Last run</th>" in html
    assert "<th>Next run</th>" in html
    assert '<span class="run-status success">Success</span>' in html
    assert "Never" in html
```

- [ ] **Step 10: Run template test and verify RED**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_job_preview.py::test_jobs_page_renders_last_and_next_run_columns
```

Expected: fail because columns are absent.

- [ ] **Step 11: Update jobs template**

In `app/templates/jobs.html`, change the configured jobs header:

```jinja2
<thead><tr><th>Name</th><th>Schedule</th><th>Next run</th><th>Last run</th><th>Status</th><th>Steps</th><th></th></tr></thead>
```

Add next run cell:

```jinja2
<td>
  {% if not job.enabled %}
    Disabled
  {% elif item.next_run %}
    {{ format_local_time(item.next_run) }}
  {% else %}
    Never
  {% endif %}
</td>
```

Add last run cell:

```jinja2
<td>
  {% if item.last_run %}
    <span class="run-status {{ item.last_run.status }}">{{ item.last_run.status|capitalize }}</span>
    {{ format_local_time(item.last_run.started_at) }}
  {% else %}
    No runs
  {% endif %}
</td>
```

Update empty row colspan to `7`.

- [ ] **Step 12: Verify jobs visibility task**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest tests/test_schedule.py::test_next_run_time_returns_none_for_never_schedule tests/test_schedule.py::test_next_run_time_returns_future_time_for_daily_schedule tests/test_web.py::test_jobs_page_includes_last_and_next_run_context tests/test_job_preview.py::test_jobs_page_renders_last_and_next_run_columns
```

Expected: pass.

- [ ] **Step 13: Commit Task 5**

```bash
git add app/scheduler.py app/main.py app/templates/jobs.html tests/test_schedule.py tests/test_web.py tests/test_job_preview.py
git commit -m "feat: show job run context"
```

---

### Task 6: Full Verification

**Files:**
- All changed files.

- [ ] **Step 1: Run full test suite**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest
```

Expected: all tests pass.

- [ ] **Step 2: Run lint and format checks**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check .
UV_CACHE_DIR=.uv-cache uv run ruff format --check .
```

Expected: both commands pass.

- [ ] **Step 3: Inspect final diff and log**

Run:

```bash
git status --short
git log --oneline -6
```

Expected: clean worktree and one commit per task after the spec/plan commits.

- [ ] **Step 4: Report verification evidence**

Final response should list:

- Commits created.
- `pytest` result.
- `ruff check` result.
- `ruff format --check` result.
- Any intentionally deferred work, namely full job import/export.

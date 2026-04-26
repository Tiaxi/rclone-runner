# Live Run Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start manual job and step runs in the background, open the step log immediately, stream appended output live, update terminal metadata, and support canceling active jobs.

**Architecture:** Persist `JobRunRecord` and `JobStepRunRecord` rows before subprocess execution starts. A `LiveJobRunner` service owns active asyncio tasks and subprocess cancellation, while existing pages poll JSON endpoints for status and appended log content.

**Tech Stack:** FastAPI, SQLAlchemy, asyncio subprocesses, Jinja templates, vanilla JavaScript, pytest.

---

### File Structure

- Modify `app/db.py`: make run fields lifecycle-aware, add status fields, and add startup migration for existing SQLite databases.
- Modify `app/core/runner.py`: add cancelable subprocess handles and a live runner service that persists start/finish state.
- Modify `app/runner_service.py`: expose the live runner instance.
- Modify `app/scheduler.py`: use the live runner path for scheduled jobs.
- Modify `app/main.py`: start manual runs immediately, add status/log append/cancel endpoints, and render running metadata.
- Modify `app/core/logs.py`: add byte-offset append reading.
- Modify `app/templates/run_detail.html`: render status-aware metadata and stop button.
- Modify `app/templates/_log_viewer.html`: poll live output and status while running.
- Modify `app/templates/job_detail.html` and `app/templates/runs.html`: display running/canceled statuses.
- Modify `app/static/styles.css`: style the stop action and running status text.
- Modify `tests/test_runner.py`: cover live persistence and cancellation.
- Modify or create web/log tests: cover status and append payload behavior.

### Task 1: Lifecycle Columns and Log Append Helper

**Files:**
- Modify: `app/db.py`
- Modify: `app/core/logs.py`
- Test: `tests/test_log_chunks.py`

- [ ] **Step 1: Write failing tests**

Add tests to `tests/test_log_chunks.py`:

```python
from app.core.logs import read_log_append


def test_reads_log_append_from_byte_offset(tmp_path: Path):
    log_path = tmp_path / "run.log"
    log_path.write_text("one\n", encoding="utf-8")
    offset = log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("two\n")

    append = read_log_append(log_path, offset=offset)

    assert append == {"text": "two\n", "offset": log_path.stat().st_size}


def test_missing_append_log_returns_empty_payload(tmp_path: Path):
    append = read_log_append(tmp_path / "missing.log", offset=12)

    assert append == {"text": "", "offset": 0}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_log_chunks.py -q`

Expected: import failure for `read_log_append`.

- [ ] **Step 3: Implement log append helper and DB lifecycle fields**

In `app/core/logs.py`, add:

```python
def read_log_append(path: Path, offset: int = 0) -> dict[str, object]:
    if not path.exists():
        return {"text": "", "offset": 0}
    file_size = path.stat().st_size
    start = max(0, min(offset, file_size))
    with path.open("rb") as log_file:
        log_file.seek(start)
        data = log_file.read()
    return {"text": _decode(data), "offset": file_size}
```

In `app/db.py`, change `JobRunRecord.ended_at`, `JobStepRunRecord.exit_code`, `JobStepRunRecord.ended_at`, `ConsoleRunRecord.exit_code`, and `ConsoleRunRecord.ended_at` to nullable where needed for running records. Add `status` columns to job and step run records with defaults. Add a small SQLite migration in `init_db()` that uses `inspect(engine)` and `ALTER TABLE` to add `status` columns if missing.

- [ ] **Step 4: Run tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_log_chunks.py -q`

Expected: all tests in the file pass.

### Task 2: Live Runner Service

**Files:**
- Modify: `app/core/runner.py`
- Modify: `app/runner_service.py`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write failing tests**

Add tests for:

```python
@pytest.mark.asyncio
async def test_live_runner_creates_step_record_before_executor_finishes():
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(argv, env, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n", encoding="utf-8")
        started.set()
        await release.wait()
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[JobStep(id=1, name="one", command="lsd secret:")],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = LiveJobRunner(Path(tmpdir), session_factory=SessionLocal, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        assert first_step.status == "running"
        assert first_step.exit_code is None
        await started.wait()
        release.set()
        await runner.wait_for_job_run(first_step.job_run_id)

        with SessionLocal() as db:
            finished = db.get(JobStepRunRecord, first_step.id)
            assert finished.status == "success"
            assert finished.exit_code == 0
            assert finished.ended_at is not None

@pytest.mark.asyncio
async def test_live_runner_cancel_marks_job_canceled_and_skips_remaining_steps():
    started = asyncio.Event()
    calls = []

    async def executor(argv, env, log_path):
        calls.append(argv)
        started.set()
        await asyncio.sleep(30)
        return 0

    job = Job(
        id=42,
        name="backup",
        common_args="",
        env={},
        steps=[
            JobStep(id=1, name="one", command="lsd secret:"),
            JobStep(id=2, name="two", command="lsd secret:/two"),
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = LiveJobRunner(Path(tmpdir), session_factory=SessionLocal, executor=executor)
        first_step = runner.start_job(job, trigger="manual")
        await started.wait()
        assert runner.cancel_job_run(first_step.job_run_id)
        await runner.wait_for_job_run(first_step.job_run_id)

        with SessionLocal() as db:
            job_run = db.get(JobRunRecord, first_step.job_run_id)
            step_runs = db.query(JobStepRunRecord).filter_by(job_run_id=job_run.id).all()
            assert job_run.status == "canceled"
            assert [step.status for step in step_runs] == ["canceled"]
            assert len(calls) == 1
```

Use a temporary SQLite database/session or the app session fixture pattern already present in tests. The first test should start a live run, assert a step record exists with `status == "running"` before releasing the executor, then assert it becomes `success` with `exit_code == 0`. The second should start a two-step job, cancel the parent job run while the first executor waits, then assert only the first step ran and both job and step statuses are `canceled`.

- [ ] **Step 2: Run tests to verify failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_runner.py -q`

Expected: failures because no live runner API exists.

- [ ] **Step 3: Implement live runner**

Add `LiveJobRunner` to `app/core/runner.py` with:

- `start_job(job, trigger, dry_run=False, step_id=None) -> JobStepRunRecord | None`
- `_run_job_background(job_run_id, job, trigger, dry_run, selected_steps, run_stamp) -> None` internal coroutine
- `cancel_job_run(job_run_id) -> bool`
- `wait_for_job_run(job_run_id) -> None` for tests and shutdown-safe coordination
- active maps for `job_id -> job_run_id`, `job_run_id -> task`, and `job_run_id -> active subprocess/process cancel callback`

Keep the existing `JobRunner.run_job()` API intact for current unit tests. Update `subprocess_executor` or add a live executor path that can terminate the subprocess when cancellation is requested.

- [ ] **Step 4: Run tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_runner.py -q`

Expected: runner tests pass.

### Task 3: Web Routes for Immediate Redirect, Status, Append, Cancel

**Files:**
- Modify: `app/main.py`
- Modify: `app/scheduler.py`
- Test: `tests/test_web.py` or new `tests/test_live_runs.py`

- [ ] **Step 1: Write failing route tests**

Add tests that call the manual run route and assert it returns a redirect to `/runs/{id}` before the executor finishes. Add direct tests for:

- `GET /runs/{run_id}/status`
- `GET /runs/{run_id}/log/append?offset=0`
- `POST /runs/{run_id}/cancel`

- [ ] **Step 2: Run tests to verify failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py tests/test_runner.py -q`

Expected: route or endpoint failures.

- [ ] **Step 3: Implement routes**

Change `_run_job()` in `app/main.py` to call `runner.start_job(...)` and redirect immediately. Add:

```python
@app.get("/runs/{run_id}/status")
async def run_status(run_id: int, _: AuthRequired, db: DbSession) -> dict[str, object]:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return {"status": "missing", "can_cancel": False}
    return _run_status_payload(run)

@app.get("/runs/{run_id}/log/append")
async def run_log_append(run_id: int, _: AuthRequired, db: DbSession, offset: int = 0) -> dict[str, object]:
    run = db.get(JobStepRunRecord, run_id)
    if run is None:
        return {"text": "", "offset": 0}
    return read_log_append(Path(run.log_path), offset)

@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, _: AuthRequired, db: DbSession) -> Response:
    run = db.get(JobStepRunRecord, run_id)
    if run is not None:
        runner.cancel_job_run(run.job_run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)
```

Update `app/scheduler.py` to call the same live runner service without expecting a return value.

- [ ] **Step 4: Run focused web tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py tests/test_runner.py -q`

Expected: focused tests pass.

### Task 4: Live Log UI

**Files:**
- Modify: `app/templates/run_detail.html`
- Modify: `app/templates/_log_viewer.html`
- Modify: `app/templates/job_detail.html`
- Modify: `app/templates/runs.html`
- Modify: `app/static/styles.css`

- [ ] **Step 1: Update templates**

Add data attributes for status and append URLs to the log viewer. Render status text instead of assuming exit code exists. Show a stop button only when `run.status == "running"`.

- [ ] **Step 2: Update JavaScript**

Extend `_log_viewer.html` JavaScript to poll append and status endpoints every second while running. Preserve older-lines loading and bottom-follow behavior.

- [ ] **Step 3: Update styles**

Add minimal styles for `.danger`, `.run-meta`, and disabled stop form state.

- [ ] **Step 4: Run template-related tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest tests/test_web.py tests/test_console_logs.py -q`

Expected: tests pass.

### Task 5: Full Verification

**Files:**
- All modified files

- [ ] **Step 1: Run formatter**

Run: `UV_CACHE_DIR=.uv-cache uv run ruff format app tests`

Expected: files formatted.

- [ ] **Step 2: Run full tests and lint**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest && UV_CACHE_DIR=.uv-cache uv run ruff check . && UV_CACHE_DIR=.uv-cache uv run ruff format --check .`

Expected: all tests pass and ruff reports no issues.

- [ ] **Step 3: Review diff**

Run: `git diff --stat && git diff -- app tests docs/superpowers/plans/2026-04-26-live-run-logs.md`

Expected: diff is scoped to live run logs, status, cancellation, and tests.

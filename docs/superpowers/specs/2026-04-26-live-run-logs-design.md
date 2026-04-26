# Live Run Logs Design

## Goal

Manual job and step runs should open their log page immediately, stream visible output while the command runs, update exit code and finish time when complete, show live elapsed time, and allow a running command to be canceled.

## Current Behavior

`POST /jobs/{job_id}/run` and step run routes await `runner.run_job(...)` before saving `JobRunRecord` and `JobStepRunRecord` rows. Because records are only persisted after completion, the app has no run detail URL to redirect to while a command is still executing.

## Architecture

Persist run records at the start of execution, then run the subprocess in a background task. The log page reads the already-created `JobStepRunRecord`, polls for appended log output and metadata, and displays terminal state once the runner updates the record.

The runner remains the owner of subprocess lifecycle and overlap prevention. The web layer starts manual runs, redirects to the first persisted step run, and exposes status and cancel endpoints.

## Data Model

`JobRunRecord` and `JobStepRunRecord` need lifecycle-friendly fields:

- `status`: `running`, `success`, `failed`, `canceled`, or `skipped`.
- `exit_code`: nullable for running rows.
- `ended_at`: nullable for running rows.

Existing completed records continue to render as finished runs.

## Execution Flow

When a manual job or step is started:

1. Validate and load the job.
2. Create a `JobRunRecord` with `status="running"` and a first `JobStepRunRecord` with `status="running"`, nullable `exit_code`, nullable `ended_at`, command argv, timestamps, and log path.
3. Start the actual execution in an asyncio background task.
4. Redirect immediately to `/runs/{step_run_id}`.

For full jobs, subsequent step rows are created as each step starts. If a step fails, later steps are not started and the job status becomes `failed`. If a run is canceled, the active subprocess is stopped, the active step and parent job are marked `canceled`, and later steps are skipped by omission.

Scheduled runs use the same live persistence path but do not need an immediate redirect.

## Live Log Page

The existing run detail page remains the canonical log page. It renders current metadata from the database and initializes the existing tail chunk.

For running rows, the page:

- Polls a metadata endpoint for status, exit code, finish time, and elapsed seconds.
- Polls a log endpoint for bytes appended since the last known offset.
- Keeps the log scrolled to the bottom when the user is already following the tail.
- Stops live polling when the run reaches a terminal status.

The existing older-lines behavior remains available for completed logs and for users who scroll upward.

## Cancellation

Running step pages show a stop button. Posting to the cancel endpoint requests cancellation for the parent job run. The runner terminates the current subprocess, records the active step as canceled, records the parent job as canceled, sets `ended_at`, and releases overlap protection for that job.

If cancellation is requested after the run has already finished, the endpoint is idempotent and redirects back to the log page without changing terminal metadata.

## Testing

Tests should cover:

- Manual run routes create records and redirect before executor completion.
- Running status responses expose nullable exit code, live elapsed time, and `can_cancel=true`.
- Finishing a run updates exit code, finish time, and terminal status.
- Canceling a full job stops the active step and prevents later steps from starting.
- Log append polling returns only content written after the requested byte offset.
- Existing completed-run rendering and history behavior remain compatible.

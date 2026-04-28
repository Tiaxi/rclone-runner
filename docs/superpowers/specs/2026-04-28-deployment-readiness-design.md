# Deployment Readiness Design

## Goal

Prepare Rclone Runner for public GitHub publication and safer self-hosted deployment without turning the app into a multi-user platform.

## Current Behavior

The app already supports the main backup workflow: login, job editing, scheduled and manual runs, dry-runs, live logs, cancellation, run history, a restricted rclone console, retention cleanup, and Docker Compose deployment.

The remaining gaps are deployment safety and a missing job deletion path. POST actions do not have CSRF protection, destructive actions do not consistently ask for confirmation, insecure default credentials only appear in documentation, there is no health endpoint, the Jobs page does not show last/next run context, and job records cannot be deleted from the UI.

## Scope

This design covers:

- Runtime warnings for insecure default configuration.
- CSRF protection for form POSTs.
- Confirmation prompts for destructive actions.
- Job deletion from the UI.
- `/health` for container and reverse-proxy health checks.
- Jobs-page operational visibility: last run and next scheduled run.
- README deployment checklist and backup guidance.

This design does not cover full job import/export. Backup guidance for `/data` and `rclone.conf` is sufficient for the first public release.

## Runtime Safety Warnings

The app should start even when defaults are insecure, because LAN-only deployments and first-run setup should remain simple. Instead, authenticated pages show a prominent warning when:

- `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` is empty, which enables the development password.
- `RCLONE_RUNNER_SECRET_KEY` is the default placeholder value.

The warning explains the risk and points to the README configuration steps. It should be generated from a small helper so templates do not duplicate configuration checks.

## CSRF Protection

The app should add a lightweight session-backed CSRF token:

1. Ensure each browser session has a random token.
2. Expose the token to templates.
3. Include it as a hidden field in POST forms.
4. Validate it in form POST handlers before mutating state.

Invalid tokens return HTTP 403. The login route should also include and validate a token, using a session token created before authentication. WebSocket console traffic remains authenticated by the session and does not use form CSRF tokens.

## Destructive Actions

Destructive forms should include browser confirmation prompts:

- Delete job.
- Delete job run.
- Delete step run.
- Delete console run.
- Clear all history.
- Prune old logs.

The prompt text should name the object or action clearly. Server-side authorization and CSRF validation remain the real protection; prompts are only an accidental-click guard.

## Job Deletion

Add `POST /jobs/{job_id}/delete`.

Deleting a job removes the job configuration and its steps. Historical job runs, step runs, console runs, and logs remain intact so past activity stays auditable.

Deletion is blocked if the job has a currently running `JobRunRecord`. In that case the route redirects back to the job page with a visible notice explaining that the running job must finish or be canceled first.

The UI should expose deletion from:

- The job detail page, near other job-level actions.
- The Jobs list, as an aligned action beside existing job controls.

## Health Endpoint

Add `GET /health`, unauthenticated, returning a small JSON payload:

```json
{"status": "ok"}
```

Docker Compose should use this endpoint as the service healthcheck. The endpoint should not touch rclone remotes or run expensive checks; it only verifies that the web process is alive and routing requests.

## Jobs-Page Visibility

The Jobs page should show enough context to decide whether a backup is healthy:

- Last run status.
- Last run start time.
- Next scheduled run for enabled scheduled jobs.

Jobs with no schedule show `Never`. Disabled jobs show that no future run is scheduled. If a cron expression cannot produce a next fire time, show `Not scheduled`.

This should reuse the existing cron normalization and scheduler timezone. A helper can compute the next fire time with the same cron trigger used by APScheduler.

## Documentation

Update README with a deployment checklist:

- Set `RCLONE_RUNNER_ADMIN_PASSWORD_HASH`.
- Set a long random `RCLONE_RUNNER_SECRET_KEY`.
- Configure `/data`, `/config/rclone`, and `/media` mounts.
- Set timezone and retention.
- Verify `/health`.
- Use HTTPS or a trusted reverse proxy if exposed beyond the LAN.
- Back up `/data` and `rclone.conf`.

The README should clearly state that the app is designed for a trusted single-admin environment, not as a public multi-user service.

## Testing

Tests should cover:

- Insecure configuration warnings are produced for missing password hash and default secret key.
- CSRF tokens are rendered in forms and invalid POSTs are rejected.
- Destructive forms include confirmation prompts.
- Job deletion removes the job and steps while keeping history.
- Job deletion is blocked while a job run is running.
- `/health` returns `{"status": "ok"}` without authentication.
- Docker Compose includes a healthcheck for `/health`.
- Jobs page renders last run and next scheduled run context.
- README includes the deployment checklist and backup guidance.

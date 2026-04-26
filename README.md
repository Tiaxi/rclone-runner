# Rclone Runner

Dockerized rclone scheduler and web UI for TrueNAS SCALE.

This app is intentionally CLI-native. It does not try to wrap every rclone feature. Configure
remotes with `rclone config` through the restricted web console, then create scheduled jobs from
raw rclone subcommands plus shared arguments and environment variables.

## Features

- Single admin login.
- Jobs with cron schedules and sequential rclone command steps.
- Job-level common rclone arguments and environment variables.
- Manual run button for every job.
- Overlap protection: a job is skipped if its previous run is still active.
- Stop-on-failure behavior for multi-step jobs.
- Run history with command argv, timestamps, exit codes, duration, and logs.
- Restricted rclone console with persisted command history.
- SQLite persistence under `/data`.

## Development

This project targets Python 3.14 and uses uv.

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run uvicorn app.main:app --reload
```

Default development login is `admin` / `admin` when no password hash is configured.

## Job Step Format

Each step is one line:

```text
Sync Musiikki|sync /media/Musiikki secret:/Musiikki
Sync Musa|sync /media/Musa secret:/Musa
```

Common args are inserted after the rclone subcommand:

```text
--fast-list --transfers=20 --checkers=40 --tpslimit=10 --drive-chunk-size=32M --max-backlog 200000 --verbose --bwlimit ${BW_LIMIT}
```

Environment variables are configured as `KEY=value` lines:

```text
BW_LIMIT=8M
```

## Docker Compose

For local development or a TrueNAS SCALE custom app using "Install via YAML":

```bash
docker compose up --build
```

Update the volume mappings in `docker-compose.yml` for your TrueNAS datasets. The important mounts
are:

- `/data`: SQLite database and run logs.
- `/config/rclone`: persistent `rclone.conf`.
- `/media`: read-only source datasets for backups.

Generate an admin password hash before exposing the app:

```bash
uv run python -c "from app.auth import hash_password; print(hash_password('your-password'))"
```

Set the result as `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` and set a long random
`RCLONE_RUNNER_SECRET_KEY`.

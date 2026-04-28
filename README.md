# Rclone Runner

Dockerized rclone scheduler and web UI for TrueNAS SCALE.

Rclone Runner is intentionally CLI-native. It does not try to wrap every rclone feature in a GUI.
Use the restricted web console for direct `rclone` access, configure remotes with `rclone config`,
then define scheduled jobs from raw rclone subcommands, shared arguments, and environment variables.

## Features

- Single admin login.
- Restricted web terminal for `rclone` commands only.
- Jobs with cron schedules or `Never` schedules.
- Sequential job steps written as raw rclone subcommands.
- Job-level common rclone arguments and environment variables.
- Manual full-job, dry-run, step-run, and step dry-run actions.
- Run history for jobs and console commands.
- Lazy-loaded log viewer with raw log access.
- SQLite persistence under `/data`.
- Docker Compose deployment suitable for TrueNAS SCALE custom apps.

## Development

This project targets Python 3.14 and uses uv.

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Default development login is `admin` / `admin` when no password hash is configured.

## Configuration

Copy the example environment file and edit it for your machine:

```bash
cp .env.example .env
```

The `.env` file is required for normal Docker Compose use. Compose reads it for the `${...}`
substitutions in `docker-compose.yml`.
The Compose service also uses `env_file: ${ENV_FILE:-.env}` so the same values are available
inside the container.

To use a non-default env file, pass it to both Compose interpolation and the container env file:

```bash
ENV_FILE=.env.production docker compose --env-file .env.production up -d
```

Generate an admin password hash before exposing the app:

```bash
uv run python -c "from app.auth import hash_password; print(hash_password('your-password'))"
```

Set the result as `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` in `.env`, and replace
`RCLONE_RUNNER_SECRET_KEY` with a long random value.

Important variables:

| Variable | Purpose |
| --- | --- |
| `RCLONE_RUNNER_HOST_PORT` | Host port mapped to the web UI. |
| `RCLONE_RUNNER_ADMIN_USER` | Login username. |
| `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` | Password hash generated with `app.auth.hash_password`. |
| `RCLONE_RUNNER_SECRET_KEY` | Session signing secret. |
| `RCLONE_RUNNER_TIMEZONE` | Timezone used for displayed timestamps. |
| `RCLONE_RUNNER_RETENTION_DAYS` | Log retention setting used by the prune action. |
| `RCLONE_CONFIG` | Path to `rclone.conf` inside the container. |
| `RCLONE_RUNNER_DATA_PATH` | Host path mounted to `/data`. |
| `RCLONE_RUNNER_RCLONE_CONFIG_PATH` | Host path mounted to `/config/rclone`. |
| `RCLONE_RUNNER_MEDIA_PATH` | Host path mounted read-only to `/media`. |

`.env` is ignored by Git. Keep real secrets, dataset paths, and deployment-specific values there.

## Email Notifications

Email notifications are configured from the Settings page. The app supports generic SMTP settings,
including Gmail with `smtp.gmail.com`, port `587`, STARTTLS, your Gmail address as the username,
and a Gmail app password as the SMTP password.

The SMTP password is stored in the SQLite database and is write-only in the UI: leaving the password
field blank keeps the saved value. You can choose which job outcomes send mail: success, failure,
and canceled runs. Failed and canceled job emails include the configured tail of the job log so the
problem is visible without opening the app.

## Docker Compose

For local development or a TrueNAS SCALE custom app using "Install via YAML":

```bash
docker compose up --build
```

The important container mounts are:

- `/data`: SQLite database and run logs.
- `/config/rclone`: persistent `rclone.conf`.
- `/media`: read-only source datasets for backups.

For TrueNAS SCALE, set these in `.env` or adapt the Compose YAML before deployment:

```dotenv
RCLONE_RUNNER_DATA_PATH=/mnt/tank/apps/rclone-runner/data
RCLONE_RUNNER_RCLONE_CONFIG_PATH=/mnt/tank/apps/rclone-runner/rclone-config
RCLONE_RUNNER_MEDIA_PATH=/mnt/tank/media
```

## Deployment Checklist

- Generate and set `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` before exposing the app.
- Replace `RCLONE_RUNNER_SECRET_KEY` with a long random value unique to the deployment.
- Confirm `/data`, `/config/rclone`, and any source dataset mounts point at persistent host paths.
- Set `RCLONE_RUNNER_TIMEZONE`, `TZ`, and `RCLONE_RUNNER_RETENTION_DAYS` for the deployment.
- Verify the service health endpoint with `curl http://127.0.0.1:${RCLONE_RUNNER_HOST_PORT:-8000}/health`, using the configured host port if `RCLONE_RUNNER_HOST_PORT` is changed.
- Put the app behind HTTPS or a trusted reverse proxy before remote access.
- Back up `/data` and `rclone.conf`; they contain the database, run logs, schedules, and rclone remote configuration.
- Run Rclone Runner only in a single-admin trusted environment.

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

## Security Notes

- The web console accepts only commands that resolve to `rclone ...`.
- Do not expose the app publicly without HTTPS and a strong password.
- Do not commit `.env`, `data/`, logs, `rclone.conf`, or generated database files.
- Be careful with `rclone config show`; it can display tokens and secrets in logs.

## License

MIT. See [LICENSE](LICENSE).

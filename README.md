# Rclone Runner

Schedule rclone backups from a web UI. Built for TrueNAS SCALE, runs in Docker.

Jobs are plain rclone commands. Rclone Runner runs them on a schedule, keeps the logs, and
emails you when something goes wrong. A built-in terminal runs rclone directly, so you set up
remotes with `rclone config` as usual.

## Features

- Jobs with a cron schedule, or run by hand only
- Each job is a list of rclone commands run in order, with shared flags and variables
- Dry runs, and running a single step
- Change the bandwidth limit or pause a job while it runs
- Source sizes and upload time estimates for each step
- Run history and logs
- Email on success, failure or cancel, over any SMTP server (Gmail works with an app password)

## Quick start

```bash
git clone https://github.com/Tiaxi/rclone-runner.git
cd rclone-runner
cp .env.example .env
```

In `.env`, set:

- `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` to the output of
  `uv run python -c "from app.auth import hash_password; print(hash_password('your-password'))"`
- `RCLONE_RUNNER_SECRET_KEY` to a long random string, such as the output of `openssl rand -hex 32`
- `RCLONE_RUNNER_MEDIA_PATH` to the folder you want to back up

Then start it and open <http://localhost:8000>:

```bash
docker compose up -d --build
```

Without a password hash the login is `admin` / `admin`. That's fine for a quick try, not for
anything else.

### TrueNAS SCALE

Clone the repository into a dataset, for example `/mnt/tank/apps/rclone-runner`, and add to `.env`:

```dotenv
# 568 is TrueNAS's built-in "apps" user
RCLONE_RUNNER_UID=568
RCLONE_RUNNER_GID=568
RCLONE_RUNNER_DATA_PATH=/mnt/tank/apps/rclone-runner/data
RCLONE_RUNNER_RCLONE_CONFIG_PATH=/mnt/tank/apps/rclone-runner/rclone-config
RCLONE_RUNNER_MEDIA_PATH=/mnt/tank/media
```

The `apps` user must be able to write to the data and rclone-config folders and read the media
folder. Then install a custom app with **Install via YAML** and paste:

```yaml
include:
  - /mnt/tank/apps/rclone-runner/docker-compose.yml
```

**Upgrading from an older version that ran as root?** Hand the files to the new user first,
while no job is running:

```bash
sudo chown -R 568:568 /mnt/tank/apps/rclone-runner/data /mnt/tank/apps/rclone-runner/rclone-config
```

## Configuration

The settings you're most likely to change. [`.env.example`](.env.example) has the rest.

| Variable | What it's for |
| --- | --- |
| `RCLONE_RUNNER_HOST_PORT` | Port for the web UI (default `8000`) |
| `RCLONE_RUNNER_UID`, `RCLONE_RUNNER_GID` | User the container runs as (default `1000:1000`) |
| `RCLONE_RUNNER_ADMIN_USER` | Login name |
| `RCLONE_RUNNER_ADMIN_PASSWORD_HASH` | Login password hash |
| `RCLONE_RUNNER_SECRET_KEY` | Signs login sessions |
| `TZ`, `RCLONE_RUNNER_TIMEZONE` | Time zone for schedules and timestamps |
| `RCLONE_RUNNER_RETENTION_DAYS` | Logs older than this are removed when you prune them in Settings (default `30`) |
| `RCLONE_RUNNER_DATA_PATH` | Host folder for the database and logs, mounted at `/data` |
| `RCLONE_RUNNER_RCLONE_CONFIG_PATH` | Host folder for `rclone.conf`, mounted at `/config/rclone` |
| `RCLONE_RUNNER_MEDIA_PATH` | Host folder to back up, mounted read-only at `/media` |

Back up the data and rclone-config folders. Together they hold your jobs, history and remotes.

## Writing jobs

The **schedule** is a standard five-field cron expression, such as `0 2 * * 1-5` for 02:00 on
weekdays. Weekdays are `0`-`7` (`0` and `7` are Sunday) or `sun`-`sat`. Set either the day of
the month or the weekday, not both. Leave it empty to run the job by hand only.

Each step is a name and an rclone command, separated by `|`:

```text
Music|sync /media/Music crypt:/Music
Photos|sync /media/Photos crypt:/Photos
```

**Common arguments** are added to every step:

```text
--transfers=4 --stats 10s --bwlimit ${BW_LIMIT}
```

Transfer steps (`sync`, `copy`, `move`, `bisync`, `delete`, `purge` and the like) log rclone's
progress every minute and its totals at the end, which the run pages and emails show. `--stats 10s`
updates the progress more often, `--verbose` also logs each file, and `--quiet` turns it all off.

**Environment variables** go one per line:

```text
BW_LIMIT=8M
```

`${RUN_DATE}` (`2026-11-15`) and `${RUN_TIME}` (`05-00-00`) are set when a run starts. For
example, `--backup-dir crypt:versions/${RUN_DATE}/Music` keeps each day's replaced and deleted
files in their own folder. An environment line such as `RUN_DATE=2026-11-01` sets it for the job
instead, and `prune-dated` then counts days from that date too, so it must be a `YYYY-MM-DD`
date in a job with a `prune-dated` step.

### Pruning dated backup folders

Those dated folders pile up. `rclone delete --min-age` can't clear them, because moved files keep
their original modification times. The built-in `prune-dated` step goes by the folder names
instead. Put it last, after the syncs:

```text
Music|sync /media/Music crypt:/Music --backup-dir crypt:versions/${RUN_DATE}/Music
Prune versions|prune-dated crypt:versions --keep-days 30 --keep-min 7
```

It lists the folders in `crypt:versions` and purges each one named `YYYY-MM-DD` that is more
than 30 days before the run's `${RUN_DATE}`, oldest first, logging each one. Anything else in
that folder is left alone.

- The newest `--keep-min` dated folders are always kept, whatever their dates, so a wrong clock
  can't empty it.
- It refuses an empty path, the root of a remote or of the file system, paths with `..`, and
  connection string options in the path (`remote,opt=x:`): set those in the remote's config.
- A dry run lists what it would purge and deletes nothing. So does `--dry-run` in the step.
  For any step, a dry run refuses to start if the step or the common arguments contain
  `--dry-run=false` (or `-n=false`), since that would make it a real run. It also refuses
  rclone commands that make changes even with `--dry-run`, such as `config delete`, `gendocs`
  or `test`, and those not known to respect it, and flags that write a file, such as
  `--output-file`, `--combined` or `--log-file`. Transfers, deletions and listings can be
  dry-run, except `bisync`, which writes its listings even in a dry run. The same goes for any
  run with `--dry-run` or `-n` in its arguments, or `RCLONE_DRY_RUN` in its environment. A dry
  run changes none of the files and remotes the command works on; rclone may still update its
  own state, such as refreshed login tokens in `rclone.conf` or a backend's cache.
- A failed purge stops the step and fails the run. The step runs only if the steps before it
  succeed, and a folder that doesn't exist yet counts as nothing to prune.
- The common arguments, and any other flags in the step, go to `rclone lsf` and `rclone purge`.
  Give a flag's value with `=` in the step, like `--b2-hard-delete=true`. Filters such as
  `--exclude`, `--files-from` or `--min-age`, and `--max-depth`, are left out, from the
  arguments and from the environment (`RCLONE_EXCLUDE`): they are meant for the syncs, and
  `rclone purge` refuses them.
- Symlinks are never followed, even with `-L`: a dated folder that is a link isn't purged,
  and links inside a purged folder are deleted as links, not what they point to.

Pausing a running job slows rclone right down instead of stopping it, so it resumes in a couple
of seconds. To stop for longer, use Stop: the next run skips files already copied.

## Security

Anyone who can log in has full control of the container and every configured remote. The
terminal and job steps only accept rclone commands, but rclone can run other programs and print
its stored credentials, so this is not a sandbox.

- Set a strong password and your own secret key.
- Don't expose it to the internet. For remote access, put it behind HTTPS or a trusted reverse
  proxy that passes the original `Host` header (or sets `X-Forwarded-Host`).
- Never commit `.env`, `data/` or `rclone.conf`.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --dev
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).

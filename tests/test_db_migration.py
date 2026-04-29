from sqlalchemy import create_engine, inspect

from app.db import Base, _copy_lifecycle_rows


def test_lifecycle_migration_derives_missing_status_from_exit_code(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE job_step_runs_old (
                id INTEGER PRIMARY KEY,
                job_run_id INTEGER NOT NULL,
                step_id INTEGER,
                step_name VARCHAR(200) NOT NULL,
                argv_json TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                log_path TEXT NOT NULL,
                started_at DATETIME NOT NULL,
                ended_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO job_step_runs_old (
                id, job_run_id, step_id, step_name, argv_json, exit_code, log_path,
                started_at, ended_at
            )
            VALUES
                (1, 1, 1, 'ok', '[]', 0, 'ok.log', '2026-04-26 12:00:00', '2026-04-26 12:00:01'),
                (2, 1, 2, 'bad', '[]', 7, 'bad.log', '2026-04-26 12:00:00', '2026-04-26 12:00:01')
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE console_runs_old (
                id INTEGER PRIMARY KEY,
                command TEXT NOT NULL,
                argv_json TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                log_path TEXT NOT NULL,
                started_at DATETIME NOT NULL,
                ended_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO console_runs_old (
                id, command, argv_json, exit_code, log_path, started_at, ended_at
            )
            VALUES
                (1, 'ok', '[]', 0, 'ok.log', '2026-04-26 12:00:00', '2026-04-26 12:00:01'),
                (2, 'bad', '[]', 7, 'bad.log', '2026-04-26 12:00:00', '2026-04-26 12:00:01')
            """
        )
        Base.metadata.tables["job_step_runs"].create(bind=connection)
        Base.metadata.tables["console_runs"].create(bind=connection)

        _copy_lifecycle_rows(connection, inspect(connection), "job_step_runs")
        _copy_lifecycle_rows(connection, inspect(connection), "console_runs")

        step_statuses = connection.exec_driver_sql(
            "SELECT id, status FROM job_step_runs ORDER BY id"
        ).all()
        console_statuses = connection.exec_driver_sql(
            "SELECT id, status FROM console_runs ORDER BY id"
        ).all()

    assert step_statuses == [(1, "success"), (2, "failed")]
    assert console_statuses == [(1, "success"), (2, "failed")]


def test_lifecycle_migration_preserves_transfer_stats_json_when_present(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE job_step_runs_old (
                id INTEGER PRIMARY KEY,
                job_run_id INTEGER NOT NULL,
                step_id INTEGER,
                step_name VARCHAR(200) NOT NULL,
                argv_json TEXT NOT NULL,
                status VARCHAR(40) NOT NULL,
                exit_code INTEGER,
                log_path TEXT NOT NULL,
                started_at DATETIME NOT NULL,
                ended_at DATETIME,
                transfer_stats_json TEXT
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO job_step_runs_old (
                id, job_run_id, step_id, step_name, argv_json, status, exit_code, log_path,
                started_at, ended_at, transfer_stats_json
            )
            VALUES (
                1, 1, 1, 'ok', '[]', 'success', 0, 'ok.log',
                '2026-04-26 12:00:00', '2026-04-26 12:00:01',
                '{"transferred_bytes": 1, "transferred_files": 2, "deleted_files": 3}'
            )
            """
        )
        Base.metadata.tables["job_step_runs"].create(bind=connection)

        _copy_lifecycle_rows(connection, inspect(connection), "job_step_runs")

        stats_values = connection.exec_driver_sql(
            "SELECT transfer_stats_json FROM job_step_runs"
        ).all()

    assert stats_values == [
        ('{"transferred_bytes": 1, "transferred_files": 2, "deleted_files": 3}',)
    ]

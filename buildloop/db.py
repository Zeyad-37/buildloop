"""SQLite store.

One database per machine at ``$BUILDLOOP_HOME/buildloop.sqlite``. Home
directory rather than inside a repo, because Steady has many worktree
checkouts and a repo-local database would give each worktree its own
fragmented history (RFC T-055 §4.4).

Every row carries ``project``. All writes are idempotent upserts, so re-running
any collector is always safe.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_run (
    run_id      INTEGER PRIMARY KEY,
    project     TEXT    NOT NULL,
    workflow    TEXT    NOT NULL,
    branch      TEXT,
    event       TEXT,
    status      TEXT,
    conclusion  TEXT,
    attempt     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    started_at  TEXT,
    updated_at  TEXT,
    queue_ms    INTEGER,
    exec_ms     INTEGER,
    jobs_synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ci_run_project_created ON ci_run (project, created_at);
CREATE INDEX IF NOT EXISTS ix_ci_run_status         ON ci_run (project, status);
CREATE INDEX IF NOT EXISTS ix_ci_run_jobs_synced    ON ci_run (project, jobs_synced, created_at);

CREATE TABLE IF NOT EXISTS ci_job (
    job_id       INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    project      TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    conclusion   TEXT,
    started_at   TEXT,
    completed_at TEXT,
    duration_ms  INTEGER,
    runner       TEXT
);
CREATE INDEX IF NOT EXISTS ix_ci_job_run     ON ci_job (run_id);
CREATE INDEX IF NOT EXISTS ix_ci_job_project ON ci_job (project, started_at);

CREATE TABLE IF NOT EXISTS ci_step (
    job_id      INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    project     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    conclusion  TEXT,
    duration_ms INTEGER,
    PRIMARY KEY (job_id, number)
);
CREATE INDEX IF NOT EXISTS ix_ci_step_project ON ci_step (project, name);

CREATE TABLE IF NOT EXISTS gradle_build (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    tasks          TEXT    NOT NULL,
    duration_ms    INTEGER,
    outcome        TEXT,
    task_count     INTEGER,
    executed       INTEGER,
    from_cache     INTEGER,
    up_to_date     INTEGER,
    config_cache   TEXT,
    measured_from  TEXT,
    exec_ms        INTEGER,
    gradle_version TEXT,
    daemon_reused  INTEGER,
    UNIQUE (project, ts, tasks, duration_ms)
);
CREATE INDEX IF NOT EXISTS ix_gradle_project_ts ON gradle_build (project, ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply the schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this buildloop "
            f"(supports {SCHEMA_VERSION}) — upgrade the tool"
        )
    conn.executescript(_SCHEMA)
    # Future migrations append here, guarded on `version`.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


@contextmanager
def open_db(path: Path):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- sync state -------------------------------------------------------------

def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- upserts ----------------------------------------------------------------

def upsert_run(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO ci_run (run_id, project, workflow, branch, event, status,
                            conclusion, attempt, created_at, started_at,
                            updated_at, queue_ms, exec_ms)
        VALUES (:run_id, :project, :workflow, :branch, :event, :status,
                :conclusion, :attempt, :created_at, :started_at,
                :updated_at, :queue_ms, :exec_ms)
        ON CONFLICT(run_id) DO UPDATE SET
            workflow   = excluded.workflow,
            branch     = excluded.branch,
            event      = excluded.event,
            status     = excluded.status,
            conclusion = excluded.conclusion,
            attempt    = excluded.attempt,
            started_at = excluded.started_at,
            updated_at = excluded.updated_at,
            queue_ms   = excluded.queue_ms,
            exec_ms    = excluded.exec_ms
        """,
        row,
    )


def upsert_job(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO ci_job (job_id, run_id, project, name, conclusion,
                            started_at, completed_at, duration_ms, runner)
        VALUES (:job_id, :run_id, :project, :name, :conclusion,
                :started_at, :completed_at, :duration_ms, :runner)
        ON CONFLICT(job_id) DO UPDATE SET
            conclusion   = excluded.conclusion,
            started_at   = excluded.started_at,
            completed_at = excluded.completed_at,
            duration_ms  = excluded.duration_ms,
            runner       = excluded.runner
        """,
        row,
    )


def upsert_step(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO ci_step (job_id, number, project, name, conclusion, duration_ms)
        VALUES (:job_id, :number, :project, :name, :conclusion, :duration_ms)
        ON CONFLICT(job_id, number) DO UPDATE SET
            name        = excluded.name,
            conclusion  = excluded.conclusion,
            duration_ms = excluded.duration_ms
        """,
        row,
    )


def mark_jobs_synced(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE ci_run SET jobs_synced = 1 WHERE run_id = ?", (run_id,))


def insert_gradle_build(conn: sqlite3.Connection, row: dict) -> bool:
    """Insert a local build row. Returns False if it was a duplicate.

    The UNIQUE constraint on (project, ts, tasks, duration_ms) is what makes
    re-ingesting the same JSONL a no-op — the byte-offset watermark is the fast
    path, this is the correctness backstop.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO gradle_build
            (project, ts, tasks, duration_ms, outcome, task_count, executed,
             from_cache, up_to_date, config_cache, measured_from, exec_ms,
             gradle_version, daemon_reused)
        VALUES (:project, :ts, :tasks, :duration_ms, :outcome, :task_count, :executed,
                :from_cache, :up_to_date, :config_cache, :measured_from, :exec_ms,
                :gradle_version, :daemon_reused)
        """,
        row,
    )
    return cur.rowcount > 0

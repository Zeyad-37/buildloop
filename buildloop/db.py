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

SCHEMA_VERSION = 2

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
    runner       TEXT,
    run_attempt  INTEGER
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

-- Why a failed job failed, read from its log. One row per failed job once
-- diagnosed; the row existing is what stops it being fetched again, so a job
-- whose log has expired still gets a row, with a NULL reason.
CREATE TABLE IF NOT EXISTS ci_failure (
    job_id    INTEGER PRIMARY KEY,
    project   TEXT    NOT NULL,
    step      TEXT,
    reason    TEXT,
    signature TEXT,
    excerpt   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ci_failure_project ON ci_failure (project, signature);

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
    # v2: ci_job.run_attempt. A fresh database gets it from the CREATE above;
    # an existing one needs the column added.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(ci_job)")}
    if "run_attempt" not in columns:
        conn.execute("ALTER TABLE ci_job ADD COLUMN run_attempt INTEGER")
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
                            started_at, completed_at, duration_ms, runner,
                            run_attempt)
        VALUES (:job_id, :run_id, :project, :name, :conclusion,
                :started_at, :completed_at, :duration_ms, :runner,
                :run_attempt)
        ON CONFLICT(job_id) DO UPDATE SET
            conclusion   = excluded.conclusion,
            started_at   = excluded.started_at,
            completed_at = excluded.completed_at,
            duration_ms  = excluded.duration_ms,
            runner       = excluded.runner,
            run_attempt  = excluded.run_attempt
        """,
        {"run_attempt": None, **row},
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


def upsert_failure(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO ci_failure (job_id, project, step, reason, signature, excerpt)
        VALUES (:job_id, :project, :step, :reason, :signature, :excerpt)
        ON CONFLICT(job_id) DO UPDATE SET
            step      = excluded.step,
            reason    = excluded.reason,
            signature = excluded.signature,
            excerpt   = excluded.excerpt
        """,
        row,
    )


# --- queries ----------------------------------------------------------------

def knock_on(r: sqlite3.Row, run: list[sqlite3.Row]) -> bool:
    """Did ``r`` start only after another unhappy job in its run had finished?

    Jobs from a different attempt of the run are not its upstream: a re-run
    that fails again is the flaky pattern worth seeing, not a knock-on of the
    first attempt. Rows stored before the attempt was recorded (NULL) are
    compared as before.
    """
    return any(
        o["job_id"] != r["job_id"] and o["completed_at"] and r["started_at"]
        and o["completed_at"] <= r["started_at"]
        and (o["run_attempt"] is None or r["run_attempt"] is None
             or o["run_attempt"] == r["run_attempt"])
        for o in run
    )


def failure_groups(conn: sqlite3.Connection, project: str, since: str) -> dict:
    """Diagnosed failures since ``since``, grouped by what went wrong.

    A job that failed only because an earlier job in the same run failed or
    was cancelled — a required-checks gate, a deploy that `needs:` the build —
    is a knock-on, not a cause. Counting it would put the gate at the top of
    the table every time the build breaks or a newer push supersedes it, so
    knock-ons are counted separately. "Earlier" is by timestamp (it started
    after the other finished), which is what `needs:` produces without
    buildloop having to read workflow files.

    Known limitation: an independent job that sat in a runner queue (a busy
    macOS pool, say) until after an unrelated job in the run had failed looks
    exactly like a `needs:` dependent, and is counted as a knock-on too. Telling
    them apart needs the workflow's dependency graph.
    """
    unhappy = conn.execute(
        """
        SELECT j.job_id, j.run_id, j.run_attempt, j.name AS job, j.conclusion,
               j.started_at, j.completed_at,
               f.job_id AS diagnosed, f.step, f.reason, f.signature, f.excerpt
        FROM ci_job j LEFT JOIN ci_failure f ON f.job_id = j.job_id
        WHERE j.project = ? AND j.conclusion IN ('failure', 'cancelled', 'timed_out')
          AND j.started_at >= ?
        ORDER BY j.started_at DESC
        """,
        (project, since),
    ).fetchall()
    failed = [r for r in unhappy if r["conclusion"] == "failure"]

    by_run: dict[int, list[sqlite3.Row]] = {}
    for r in unhappy:
        by_run.setdefault(r["run_id"], []).append(r)

    groups: dict[str, dict] = {}
    out = {"groups": [], "knock_on": 0, "no_log": 0, "pending": 0, "total": len(failed)}
    for r in failed:  # newest first, so a group's first row is its latest
        if r["diagnosed"] is None:
            out["pending"] += 1
            continue
        if knock_on(r, by_run[r["run_id"]]):
            out["knock_on"] += 1
            continue
        if not r["signature"]:
            out["no_log"] += 1
            continue
        g = groups.get(r["signature"])
        if g is None:
            g = groups[r["signature"]] = {
                "reason": r["reason"], "excerpt": r["excerpt"], "count": 0, "where": {},
                "last_seen": r["started_at"], "run_id": r["run_id"], "job_id": r["job_id"],
            }
        g["count"] += 1
        where = f"{r['job']} › {r['step']}" if r["step"] else r["job"]
        g["where"][where] = g["where"].get(where, 0) + 1
    # Groups were created newest first and the sort is stable, so equal
    # counts stay newest first.
    out["groups"] = sorted(groups.values(), key=lambda g: -g["count"])
    return out


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

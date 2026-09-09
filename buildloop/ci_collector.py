"""GitHub Actions collector.

Backfill is deliberately asymmetric (RFC T-055 §4.2). Runs paginate 100 at a
time, so all history costs ~45 requests. Jobs cost *one request per run*, so a
full-history job backfill would be thousands of requests and 20+ minutes for
detail that only answers current-state questions. Runs therefore go back
forever; jobs and steps are confined to a rolling window.

Two derived quantities GitHub's own UI does not show, and which are much of
the point:

    queue time     = run_started_at - created_at
    execution time = updated_at     - run_started_at

A run that slowed because the runner queue was busy is a completely different
problem from one that slowed because the build got slower.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import db, gh
from .config import Project

#: GitHub caps any single Actions-runs query at 1000 results regardless of
#: pagination, so wide windows must be bisected until they fit.
SEARCH_CAP = 1000
PAGE_SIZE = 100

#: Rolling window for jobs + steps. Steps ride along inside the jobs response
#: at no extra request cost, so bounding jobs bounds steps by construction.
JOB_WINDOW_DAYS = 90

TERMINAL_STATUS = "completed"


@dataclass
class CollectStats:
    runs: int = 0
    jobs: int = 0
    steps: int = 0
    requests: int = 0

    def __str__(self) -> str:
        return (
            f"{self.runs} runs, {self.jobs} jobs, {self.steps} steps "
            f"({self.requests} API requests)"
        )


# --- time helpers -----------------------------------------------------------

def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def delta_ms(start: str | None, end: str | None) -> int | None:
    """Milliseconds between two ISO-8601 instants, or None if unknowable.

    Negative results are clamped to None rather than stored: GitHub
    occasionally reports a ``run_started_at`` fractionally before
    ``created_at``, and a negative queue time is a lie, not a datum.
    """
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    ms = int((b - a).total_seconds() * 1000)
    return ms if ms >= 0 else None


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- run mapping ------------------------------------------------------------

def map_run(raw: dict, project: str) -> dict:
    """Map a GitHub run payload to a ``ci_run`` row.

    Discards the ~13 KB of ``repository`` / ``pull_requests`` blobs that make
    up most of the response and none of the signal.
    """
    created = raw.get("created_at")
    started = raw.get("run_started_at") or created
    updated = raw.get("updated_at")
    return {
        "run_id": int(raw["id"]),
        "project": project,
        "workflow": raw.get("name") or "(unnamed)",
        "branch": raw.get("head_branch"),
        "event": raw.get("event"),
        "status": raw.get("status"),
        "conclusion": raw.get("conclusion"),
        "attempt": int(raw.get("run_attempt") or 1),
        "created_at": created,
        "started_at": started,
        "updated_at": updated,
        "queue_ms": delta_ms(created, started),
        "exec_ms": delta_ms(started, updated),
    }


def map_job(raw: dict, project: str) -> dict:
    started = raw.get("started_at")
    completed = raw.get("completed_at")
    labels = raw.get("labels") or []
    runner = ",".join(labels) if labels else raw.get("runner_name")
    return {
        "job_id": int(raw["id"]),
        "run_id": int(raw["run_id"]),
        "project": project,
        "name": raw.get("name") or "(unnamed)",
        "conclusion": raw.get("conclusion"),
        "started_at": started,
        "completed_at": completed,
        "duration_ms": delta_ms(started, completed),
        "runner": runner,
    }


def map_steps(job_raw: dict, project: str) -> list[dict]:
    rows = []
    for step in job_raw.get("steps") or []:
        number = step.get("number")
        if number is None:
            continue
        rows.append(
            {
                "job_id": int(job_raw["id"]),
                "number": int(number),
                "project": project,
                "name": step.get("name") or "(unnamed)",
                "conclusion": step.get("conclusion"),
                "duration_ms": delta_ms(step.get("started_at"), step.get("completed_at")),
            }
        )
    return rows


# --- fetching ---------------------------------------------------------------

class RunFetcher:
    """Pages the runs endpoint, bisecting date windows that exceed the cap."""

    def __init__(self, repo: str, stats: CollectStats, log=lambda _m: None):
        self.repo = repo
        self.stats = stats
        self.log = log

    def _page(self, since: date, until: date, page: int) -> dict:
        self.stats.requests += 1
        return gh.api(  # type: ignore[return-value]
            f"repos/{self.repo}/actions/runs",
            {
                "per_page": PAGE_SIZE,
                "page": page,
                "created": f"{since.isoformat()}..{until.isoformat()}",
            },
        )

    def iter_runs(self, since: date, until: date):
        windows = [(since, until)]
        while windows:
            a, b = windows.pop()
            if a > b:
                continue
            first = self._page(a, b, 1)
            total = int(first.get("total_count") or 0)

            if total > SEARCH_CAP and a < b:
                mid = a + (b - a) // 2
                windows.append((a, mid))
                windows.append((mid + timedelta(days=1), b))
                continue
            if total > SEARCH_CAP:
                # A single day over the cap cannot be split further. Take what
                # we can and say so, rather than silently dropping runs.
                self.log(f"  ! {a} has {total} runs; only the newest {SEARCH_CAP} are reachable")

            yield from first.get("workflow_runs") or []
            reachable = min(total, SEARCH_CAP)
            for page in range(2, (reachable + PAGE_SIZE - 1) // PAGE_SIZE + 1):
                yield from self._page(a, b, page).get("workflow_runs") or []


def repo_created_date(repo: str) -> date:
    """Earliest date worth querying — the repository's own creation date."""
    data = gh.api(f"repos/{repo}")
    created = parse_ts(data.get("created_at"))  # type: ignore[union-attr]
    return created.date() if created else date(2020, 1, 1)


# --- collection -------------------------------------------------------------

def collect(conn: sqlite3.Connection, project: Project, *, log=print,
            job_window_days: int = JOB_WINDOW_DAYS) -> CollectStats:
    """Sync one project's CI data. Idempotent; safe to re-run at any time."""
    if not project.github_repo:
        return CollectStats()

    gh.ensure_available()
    stats = CollectStats()
    repo = project.github_repo
    watermark_key = f"ci_watermark:{project.name}"

    watermark = db.get_state(conn, watermark_key)
    today = _today()
    if watermark:
        wm = parse_ts(watermark)
        # Overlap by a day: GitHub's `created` filter is date-granular, so
        # starting exactly at the watermark date could skip same-day runs.
        since = (wm.date() - timedelta(days=1)) if wm else repo_created_date(repo)
        log(f"  runs: incremental since {since}")
    else:
        since = repo_created_date(repo)
        log(f"  runs: full backfill from {since} (first run — this takes a minute)")

    fetcher = RunFetcher(repo, stats, log)
    newest = watermark
    for raw in fetcher.iter_runs(since, today):
        row = map_run(raw, project.name)
        db.upsert_run(conn, row)
        stats.runs += 1
        if row["created_at"] and (newest is None or row["created_at"] > newest):
            newest = row["created_at"]
        if stats.runs % 500 == 0:
            log(f"    {stats.runs} runs…")
            conn.commit()

    # Re-fetch anything still non-terminal so its conclusion and duration land.
    for row in conn.execute(
        "SELECT run_id FROM ci_run WHERE project = ? AND status IS NOT ?",
        (project.name, TERMINAL_STATUS),
    ).fetchall():
        try:
            stats.requests += 1
            raw = gh.api(f"repos/{repo}/actions/runs/{row['run_id']}")
        except gh.GhNotFound:
            continue  # deleted or expired run; the stored row is all we get
        db.upsert_run(conn, map_run(raw, project.name))  # type: ignore[arg-type]

    if newest:
        db.set_state(conn, watermark_key, newest)
    conn.commit()

    _collect_jobs(conn, project, stats, log, job_window_days)
    conn.commit()
    return stats


def _collect_jobs(conn, project: Project, stats: CollectStats, log, window_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    pending = conn.execute(
        """
        SELECT run_id FROM ci_run
        WHERE project = ? AND created_at >= ?
          AND (jobs_synced = 0 OR status IS NOT ?)
        ORDER BY created_at DESC
        """,
        (project.name, cutoff, TERMINAL_STATUS),
    ).fetchall()

    if not pending:
        return
    log(f"  jobs: {len(pending)} run(s) in the last {window_days}d need detail")

    for i, row in enumerate(pending, 1):
        run_id = row["run_id"]
        try:
            for raw_job in _iter_jobs(project.github_repo, run_id, stats):
                db.upsert_job(conn, map_job(raw_job, project.name))
                stats.jobs += 1
                for step in map_steps(raw_job, project.name):
                    db.upsert_step(conn, step)
                    stats.steps += 1
            db.mark_jobs_synced(conn, run_id)
        except gh.GhNotFound:
            db.mark_jobs_synced(conn, run_id)  # logs expired; never ask again
        if i % 100 == 0:
            log(f"    {i}/{len(pending)} runs…")
            conn.commit()


def _iter_jobs(repo: str, run_id: int, stats: CollectStats):
    page = 1
    while True:
        stats.requests += 1
        data = gh.api(
            f"repos/{repo}/actions/runs/{run_id}/jobs",
            {"per_page": PAGE_SIZE, "page": page, "filter": "latest"},
        )
        jobs = data.get("jobs") or []  # type: ignore[union-attr]
        yield from jobs
        if len(jobs) < PAGE_SIZE:
            return
        page += 1

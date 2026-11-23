"""CI minutes: runner time, counted the way GitHub Actions bills it.

Not the same quantity as run duration. A run's duration is wall-clock time; its
minutes are the sum of its jobs' durations, so a run whose five jobs run in
parallel for ten minutes takes ten minutes and uses fifty. GitHub also rounds
every job up to the next whole minute, so a 5-second lint job costs a full
minute — which is how a workflow of many small jobs ends up costing more than
its durations suggest.

Minutes are split by runner OS rather than multiplied into a single figure:
macOS and Windows minutes are billed at a higher rate than Linux, and the rates
belong to GitHub's price list, which changes, not to this tool.

Computed from ``ci_job``, so it covers the rolling job window plus whatever has
accumulated since the first refresh — not the full run history. Re-runs are
counted as refreshes observed them. Jobs are fetched per run with
``filter=latest`` and stored by ``job_id``, a re-run attempt gets new job IDs,
and a run is only re-fetched while it is unsynced or not yet completed. So a
run first collected after its re-run counts only its latest attempt; one
collected before the re-run and re-synced while it was running keeps every
attempt it saw; one whose re-run finished between refreshes counts only its
first attempt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from . import db

LINUX, WINDOWS, MACOS, SELF_HOSTED, OTHER = "Linux", "Windows", "macOS", "self-hosted", "other"

#: Series order on the chart and in summaries: GitHub-hosted first, cheapest
#: first, so the expensive bands sit on top of the stack.
OS_ORDER = (LINUX, WINDOWS, MACOS, SELF_HOSTED, OTHER)

#: Where a job or a run ran. A hosted runner is anyone else's machine, billed
#: by the minute; a self-hosted one is your own. A run is mixed when its jobs
#: were split between the two.
HOSTED, MIXED = "hosted", "mixed"
WHERE_ORDER = (HOSTED, SELF_HOSTED, MIXED)


def runner_os(runner: str | None) -> str:
    """Classify a ``ci_job.runner`` value (comma-joined labels).

    Self-hosted wins over everything else: a self-hosted runner also carries
    an OS label (``self-hosted,linux,x64``), but it is a different bill.
    """
    labels = [label.strip().lower() for label in (runner or "").split(",") if label.strip()]
    if "self-hosted" in labels:
        return SELF_HOSTED
    for label in labels:
        if label.startswith(("ubuntu", "linux")):
            return LINUX
        if label.startswith("windows"):
            return WINDOWS
        if label.startswith("macos"):
            return MACOS
    return OTHER


def runner_where(runner: str | None) -> str:
    """Hosted or self-hosted, from a ``ci_job.runner`` value.

    Anything without the ``self-hosted`` label counts as hosted, third-party
    runner services included: GitHub adds that label to every self-hosted
    runner, so its absence is the reliable half of the test.
    """
    return SELF_HOSTED if runner_os(runner) == SELF_HOSTED else HOSTED


def runs_where(rows: Iterable[sqlite3.Row]) -> dict[int, tuple[str, str]]:
    """Each run in ``rows`` (from ``jobs``) as ``(first job start, where)``.

    Decided by the jobs that used a runner: a skipped job (which ``jobs``
    leaves out) and a job GitHub refused to start for want of budget both
    carry the label they asked for but never ran anywhere. A refused job still
    marks the run's start, and a run whose every job was refused is hosted:
    only hosted minutes have a budget.
    """
    seen: dict[int, tuple[str, set[str]]] = {}
    for r in rows:  # oldest first, so the first row of a run is its start
        _, kinds = seen.setdefault(r["run_id"], (r["started_at"], set()))
        if r["cause"] != db.BUDGET:
            kinds.add(runner_where(r["runner"]))
    return {run_id: (start, MIXED if len(kinds) > 1 else next(iter(kinds), HOSTED))
            for run_id, (start, kinds) in seen.items()}


def billed(duration_ms: int | None) -> int:
    """Whole minutes for one job: rounded up, as GitHub bills it."""
    if not duration_ms or duration_ms <= 0:
        return 0
    return -(-duration_ms // 60_000)


def jobs(conn: sqlite3.Connection, project: str, since: str | None = None) -> list[sqlite3.Row]:
    """Every job that used a runner, oldest first, with its run's workflow.

    Skipped jobs never get a runner and have no duration; they cost nothing
    and are left out here rather than counted as zero-minute jobs. ``cause``
    is the job's ``ci_failure.cause``, so a caller can drop jobs GitHub
    refused to start.
    """
    return conn.execute(
        """
        SELECT j.run_id, j.name, j.conclusion, j.started_at, j.duration_ms, j.runner,
               r.workflow, f.cause
        FROM ci_job j JOIN ci_run r ON r.run_id = j.run_id
        LEFT JOIN ci_failure f ON f.job_id = j.job_id
        WHERE j.project = ? AND j.duration_ms > 0 AND j.started_at >= ?
        ORDER BY j.started_at
        """,
        (project, since or ""),
    ).fetchall()


def totals(rows: Iterable[sqlite3.Row]) -> dict[str, Any]:
    """Minutes in ``rows``: overall, by runner OS, by where they ran and by
    workflow.

    The breakdowns are sorted largest first, and an OS with no minutes is
    left out rather than reported as zero.
    """
    by_os: dict[str, int] = {}
    by_where: dict[str, int] = {}
    by_workflow: dict[str, int] = {}
    for r in rows:
        n = billed(r["duration_ms"])
        os_ = runner_os(r["runner"])
        by_os[os_] = by_os.get(os_, 0) + n
        where = runner_where(r["runner"])
        by_where[where] = by_where.get(where, 0) + n
        by_workflow[r["workflow"]] = by_workflow.get(r["workflow"], 0) + n
    return {
        "total": sum(by_os.values()),
        "by_os": dict(sorted(by_os.items(), key=lambda kv: -kv[1])),
        "by_where": dict(sorted(by_where.items(), key=lambda kv: -kv[1])),
        "by_workflow": dict(sorted(by_workflow.items(), key=lambda kv: -kv[1])),
    }

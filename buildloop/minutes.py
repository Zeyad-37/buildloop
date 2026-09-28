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
accumulated since the first refresh — not the full run history. Jobs are
fetched with ``filter=latest``, so a job that was re-run counts only its latest
attempt; the minutes its earlier attempts used are not counted.
"""

from __future__ import annotations

import sqlite3

LINUX, WINDOWS, MACOS, SELF_HOSTED, OTHER = "Linux", "Windows", "macOS", "self-hosted", "other"

#: Series order on the chart and in summaries: GitHub-hosted first, cheapest
#: first, so the expensive bands sit on top of the stack.
OS_ORDER = (LINUX, WINDOWS, MACOS, SELF_HOSTED, OTHER)


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


def billed(duration_ms: int | None) -> int:
    """Whole minutes for one job: rounded up, as GitHub bills it."""
    if not duration_ms or duration_ms <= 0:
        return 0
    return -(-duration_ms // 60_000)


def jobs(conn: sqlite3.Connection, project: str, since: str | None = None) -> list[sqlite3.Row]:
    """Every job that used a runner, oldest first, with its run's workflow.

    Skipped jobs never get a runner and have no duration; they cost nothing
    and are left out here rather than counted as zero-minute jobs.
    """
    return conn.execute(
        """
        SELECT j.started_at, j.duration_ms, j.runner, r.workflow
        FROM ci_job j JOIN ci_run r ON r.run_id = j.run_id
        WHERE j.project = ? AND j.duration_ms > 0 AND j.started_at >= ?
        ORDER BY j.started_at
        """,
        (project, since or ""),
    ).fetchall()


def totals(rows) -> dict:
    """Minutes in ``rows``: overall, by runner OS and by workflow.

    Both breakdowns are sorted largest first, and an OS with no minutes is
    left out rather than reported as zero.
    """
    by_os: dict[str, int] = {}
    by_workflow: dict[str, int] = {}
    for r in rows:
        n = billed(r["duration_ms"])
        os_ = runner_os(r["runner"])
        by_os[os_] = by_os.get(os_, 0) + n
        by_workflow[r["workflow"]] = by_workflow.get(r["workflow"], 0) + n
    return {
        "total": sum(by_os.values()),
        "by_os": dict(sorted(by_os.items(), key=lambda kv: -kv[1])),
        "by_workflow": dict(sorted(by_workflow.items(), key=lambda kv: -kv[1])),
    }

"""Weekly duration trends below workflow level: per CI step, per local task set.

The workflow series (``ci_weekly_last_26_weeks``) says *that* CI got slower;
these say *which step* did. Same shape for local builds: the summary's
``task_sets`` gives one lifetime median per task set, which cannot show a
slowdown at all.

Every bucket carries its sample count, because a week with two samples moves a
median far more than a real regression does.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .dashboard import percentile, week_start
from .humanize import ms

WEEKS = 26
#: Steps ranked by total successful time over the window. Eight covers the
#: steps that make up nearly all of a run without flooding the context.
TOP_STEPS = 8
TOP_TASK_SETS = 6

#: A local build is a wall-clock outlier — almost always the machine sleeping
#: mid-build, or a build left waiting — when it took longer than
#: ``OUTLIER_CAP_MS`` outright, or longer than ``OUTLIER_FLOOR_MS`` *and*
#: ``OUTLIER_RATIO`` times the median of its own series (same task set, same
#: ``measured_from``, all stored history). The floor stops a 40-second build
#: of a 3-second task set being called an outlier; the ratio lets a genuinely
#: long test run stay in; the cap catches series too short to have a median.
OUTLIER_CAP_MS = 6 * 3600 * 1000
OUTLIER_FLOOR_MS = 3600 * 1000
OUTLIER_RATIO = 10
OUTLIER_RULE = (
    f"a build is excluded from median/p90 when it took over {OUTLIER_CAP_MS // 3_600_000}h, "
    f"or over {OUTLIER_FLOOR_MS // 3_600_000}h and over {OUTLIER_RATIO}x the median of the "
    "same task set and measured_from across all stored builds (machine sleep, not build time)"
)

MEASURES = ("build_start", "execution")


def window_start(today: date | None = None, weeks: int = WEEKS) -> str:
    """Monday ``weeks - 1`` weeks before this one: whole weeks only, so the
    first bucket is not a partial week that reads as a quiet one."""
    today = today or datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return (monday - timedelta(weeks=weeks - 1)).isoformat()


# --- CI steps ---------------------------------------------------------------

def ci_step_weekly(conn: sqlite3.Connection, project: str, *,
                   since: str | None = None, top: int = TOP_STEPS) -> dict | None:
    """Weekly median and p90 per (workflow, job, step), for the steps that
    took the most total time.

    Only steps that ran and succeeded count: a skipped step records ~0s and a
    step of a job that never started records nothing useful, and either would
    drag the median down to look like a speed-up. A step has no start time of
    its own, so it is bucketed by its job's ``started_at``.
    """
    since = since or window_start()
    rows = conn.execute(
        """
        SELECT r.workflow, j.name AS job, s.name AS step, j.started_at, s.duration_ms
        FROM ci_step s
        JOIN ci_job j ON j.job_id = s.job_id
        JOIN ci_run r ON r.run_id = j.run_id
        WHERE s.project = ? AND s.conclusion = 'success' AND s.duration_ms IS NOT NULL
          AND j.started_at IS NOT NULL AND j.started_at >= ?
        """,
        (project, since),
    ).fetchall()
    if not rows:
        return None

    series: dict[tuple[str, str, str], dict[str, list[int]]] = {}
    totals: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r["workflow"], r["job"], r["step"])
        wk = week_start(r["started_at"])
        if wk is None:
            continue
        series.setdefault(key, {}).setdefault(wk, []).append(r["duration_ms"])
        totals[key] = totals.get(key, 0) + r["duration_ms"]

    ranked = sorted(totals, key=lambda k: -totals[k])[:top]
    history = conn.execute(
        "SELECT MIN(j.started_at) FROM ci_step s JOIN ci_job j ON j.job_id = s.job_id "
        "WHERE s.project = ?",
        (project,),
    ).fetchone()[0]
    return {
        "counts": "successful steps of jobs that started; skipped, failed and cancelled "
                  "steps are left out. Bucketed by the job's start.",
        "step_history_from": history,
        "steps": [
            {
                "workflow": k[0], "job": k[1], "step": k[2],
                "total_time": ms(totals[k]),
                "weeks": [
                    {"week_of": wk, "samples": len(v),
                     "median": ms(percentile(v, 0.5)), "p90": ms(percentile(v, 0.9))}
                    for wk, v in sorted(series[k].items())
                ],
            }
            for k in ranked
        ],
    }


# --- local task sets --------------------------------------------------------

def outliers(builds) -> set[int]:
    """Row ids of wall-clock outliers among ``builds`` (see ``OUTLIER_RULE``).

    ``builds`` must hold every stored build of the series it is judging, not
    just a window: the reference median is the series' whole history.
    """
    by_series: dict[tuple, list[int]] = {}
    for b in builds:
        if b["duration_ms"] is not None and b["duration_ms"] <= OUTLIER_CAP_MS:
            by_series.setdefault((b["tasks"], b["measured_from"]), []).append(b["duration_ms"])
    reference = {k: percentile(v, 0.5) for k, v in by_series.items()}

    flagged = set()
    for b in builds:
        d = b["duration_ms"]
        if d is None:
            continue
        ref = reference.get((b["tasks"], b["measured_from"]))
        if d > OUTLIER_CAP_MS or (d > OUTLIER_FLOOR_MS and ref and d > OUTLIER_RATIO * ref):
            flagged.add(b["id"])
    return flagged


def local_task_set_weekly(conn: sqlite3.Connection, project: str, *,
                          since: str | None = None, top: int = TOP_TASK_SETS) -> dict | None:
    """Weekly series for the most-run task sets, one series per
    ``measured_from``.

    Builds measured from build start include configuration; builds measured
    from execution (a configuration-cache hit) do not. They are different
    quantities, so a task set run both ways is two series, never one.

    ``runs`` and ``failed`` count every build in the week. ``samples``,
    ``median`` and ``p90`` use successful builds that are not wall-clock
    outliers: a failed build stops early and reads as a fast one.
    """
    since = since or window_start()
    builds = conn.execute(
        "SELECT id, ts, tasks, duration_ms, measured_from, outcome FROM gradle_build "
        "WHERE project = ? AND measured_from IN (?, ?)",
        (project, *MEASURES),
    ).fetchall()
    if not builds:
        return None
    flagged = outliers(builds)

    recent = [b for b in builds if b["ts"] >= since]
    runs: dict[tuple[str, str], int] = {}
    for b in recent:
        key = (b["tasks"], b["measured_from"])
        runs[key] = runs.get(key, 0) + 1
    ranked = sorted(runs, key=lambda k: (-runs[k], k))[:top]

    out = []
    for key in ranked:
        weeks: dict[str, list] = {}
        for b in recent:
            if (b["tasks"], b["measured_from"]) == key:
                weeks.setdefault(week_start(b["ts"]), []).append(b)
        rows = []
        for wk in sorted(w for w in weeks if w):
            group = weeks[wk]
            excluded = [b for b in group if b["id"] in flagged]
            durations = [b["duration_ms"] for b in group
                         if b["outcome"] == "success" and b["duration_ms"] is not None
                         and b["id"] not in flagged]
            rows.append({
                "week_of": wk,
                "runs": len(group),
                "failed": sum(1 for b in group if b["outcome"] == "failed"),
                "samples": len(durations),
                "median": ms(percentile(durations, 0.5)),
                "p90": ms(percentile(durations, 0.9)),
                "outliers_excluded": len(excluded),
            })
        out.append({"tasks": key[0], "measured_from": key[1], "runs": runs[key], "weeks": rows})

    return {
        "outlier_rule": OUTLIER_RULE,
        "durations_from": "successful builds only; runs and failed count every build",
        "task_sets": out,
    }


# --- individual Gradle tasks ------------------------------------------------

TOP_TASKS = 8


def gradle_task_weekly(conn: sqlite3.Connection, project: str, *,
                       since: str | None = None, top: int = TOP_TASKS) -> dict | None:
    """Weekly series for the individual tasks that took the most total time.

    A task's own duration does not depend on how the build was measured, so
    unlike task sets this is one series per task path. ``median`` and ``p90``
    are over runs where the task actually executed and succeeded — a cache
    hit or an up-to-date check is a different, much shorter operation — and
    leave out tasks of builds flagged as wall-clock outliers. The outcome
    split counts every recorded run.
    """
    since = since or window_start()
    rows = conn.execute(
        """
        SELECT t.build_id, t.path, t.outcome, t.duration_ms, b.ts
        FROM gradle_task t JOIN gradle_build b ON b.id = t.build_id
        WHERE t.project = ? AND b.ts >= ?
        """,
        (project, since),
    ).fetchall()
    if not rows:
        return None
    builds = conn.execute(
        "SELECT id, tasks, duration_ms, measured_from FROM gradle_build WHERE project = ?",
        (project,),
    ).fetchall()
    flagged = outliers(builds)

    totals: dict[str, int] = {}
    for r in rows:
        totals[r["path"]] = totals.get(r["path"], 0) + r["duration_ms"]
    ranked = sorted(totals, key=lambda p: (-totals[p], p))[:top]
    wanted = set(ranked)

    weeks: dict[str, dict[str, list]] = {p: {} for p in ranked}
    for r in rows:
        if r["path"] in wanted:
            weeks[r["path"]].setdefault(week_start(r["ts"]), []).append(r)

    out = []
    for path in ranked:
        series = []
        for wk in sorted(w for w in weeks[path] if w):
            group = weeks[path][wk]
            timed = [r["duration_ms"] for r in group
                     if r["outcome"] == "executed" and r["build_id"] not in flagged]
            split = {o: sum(1 for r in group if r["outcome"] == o)
                     for o in ("executed", "from_cache", "up_to_date", "failed")}
            series.append({
                "week_of": wk,
                "runs": len(group),
                "samples": len(timed),
                "median_executed": ms(percentile(timed, 0.5)),
                "p90_executed": ms(percentile(timed, 0.9)),
                **split,
            })
        out.append({"task": path, "total_time": ms(totals[path]), "weeks": series})

    history = conn.execute(
        "SELECT MIN(b.ts) FROM gradle_task t JOIN gradle_build b ON b.id = t.build_id "
        "WHERE t.project = ?",
        (project,),
    ).fetchone()[0]
    return {
        "recorded": "only task runs that took at least 100ms; faster ones (mostly "
                    "up-to-date checks) are counted per build, not per task, so "
                    "up_to_date here under-counts them",
        "durations_from": "executed, successful runs, outlier builds excluded",
        "task_history_from": history,
        "tasks": out,
    }

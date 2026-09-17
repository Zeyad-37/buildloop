"""Narrative analysis of the trends, written by Claude.

The charts show *what* the numbers are; this says what they appear to mean.

Same shape as the CI collector's relationship with `gh`: shell out to a CLI the
user has already authenticated, so buildloop handles no API key and adds no
Python dependency. If the `claude` binary is absent, or the call fails, or it
times out, the page renders exactly as before — this is an enhancement, never a
prerequisite.

Two things keep it cheap and honest:

* **Cached on the data, not the clock.** The summary is hashed; the model is
  only called when the numbers actually changed. Re-running `refresh` on a
  quiet afternoon costs nothing.
* **The model never sees raw rows, only a computed summary.** Every number in
  the summary is calculated here, in Python, so the narrative is commentary on
  arithmetic that already happened rather than arithmetic done by a model.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import claude_cli, db
from .humanize import ms

STATE_PREFIX = "insight:"
DEFAULT_TIMEOUT = claude_cli.DEFAULT_TIMEOUT

#: Comparison windows. Four weeks is long enough to survive a quiet week and
#: short enough that "recent" still means recent.
WINDOW_DAYS = 28


# --- summary ----------------------------------------------------------------

def _iso_days_ago(days: int) -> str:
    """Window boundary, floored to midnight UTC.

    Deliberately not `now - days`. A boundary that moves every second shuffles
    runs between the recent and previous buckets on every refresh, so the
    summary fingerprint never repeats and the cache never hits — turning a
    free re-render into a model call every time. Day granularity is ample for
    a 28-day window.
    """
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def summarize(conn: sqlite3.Connection, project: str) -> dict:
    """Compute the factual summary the narrative is written from.

    Deliberately small: a model given 3,000 rows will summarise them badly and
    slowly. A model given twenty pre-computed comparisons has nothing to do but
    interpret them.
    """
    recent, prior = _iso_days_ago(WINDOW_DAYS), _iso_days_ago(WINDOW_DAYS * 2)

    summary: dict = {"project": project, "window_days": WINDOW_DAYS}

    runs = conn.execute(
        "SELECT workflow, conclusion, created_at, queue_ms, exec_ms FROM ci_run WHERE project = ?",
        (project,),
    ).fetchall()
    summary["ci"] = _summarize_ci(runs, recent, prior) if runs else None
    if runs:
        summary["ci"]["flaky_jobs"] = _flaky_jobs(conn, project)
        summary["ci"]["slow_steps"] = _slow_steps(conn, project)

    builds = conn.execute(
        "SELECT ts, tasks, duration_ms, measured_from, outcome, config_cache, "
        "       executed, from_cache, up_to_date "
        "FROM gradle_build WHERE project = ?",
        (project,),
    ).fetchall()
    summary["local"] = _summarize_local(builds, recent, prior) if builds else None
    return summary


def _bucket(rows, recent: str, prior: str, key="created_at"):
    now_rows = [r for r in rows if r[key] and r[key] >= recent]
    then_rows = [r for r in rows if r[key] and prior <= r[key] < recent]
    return now_rows, then_rows


def _summarize_ci(runs, recent, prior) -> dict:
    now_rows, then_rows = _bucket(runs, recent, prior)
    workflows: dict[str, dict] = {}
    tally: dict[str, int] = {}
    for r in runs:
        tally[r["workflow"]] = tally.get(r["workflow"], 0) + 1

    for wf, _ in sorted(tally.items(), key=lambda kv: -kv[1])[:5]:
        entry = {}
        for label, rows in (("recent", now_rows), ("previous", then_rows)):
            subset = [r for r in rows if r["workflow"] == wf]
            durations = [r["exec_ms"] for r in subset if r["exec_ms"] is not None]
            queues = [r["queue_ms"] for r in subset if r["queue_ms"] is not None]
            concluded = [r for r in subset if r["conclusion"]]
            entry[label] = {
                "runs": len(subset),
                "median_exec": ms(_median(durations)),
                "median_queue": ms(_median(queues)),
                "failure_rate_pct": _pct(
                    sum(1 for r in concluded if r["conclusion"] == "failure"), len(concluded)
                ),
            }
        workflows[wf] = entry

    dates = [r["created_at"] for r in runs if r["created_at"]]
    return {
        "total_runs": len(runs),
        "history_from": min(dates) if dates else None,
        "runs_recent": len(now_rows),
        "runs_previous": len(then_rows),
        "workflows": workflows,
    }


def _flaky_jobs(conn, project) -> list[dict]:
    rows = conn.execute(
        """
        SELECT name,
               SUM(CASE WHEN conclusion = 'failure' THEN 1 ELSE 0 END) AS failures,
               COUNT(*) AS total
        FROM ci_job WHERE project = ? AND conclusion IS NOT NULL
        GROUP BY name HAVING failures > 0
        ORDER BY failures DESC LIMIT 6
        """,
        (project,),
    ).fetchall()
    return [
        {"job": r["name"], "failures": r["failures"], "runs": r["total"],
         "failure_rate_pct": _pct(r["failures"], r["total"])}
        for r in rows
    ]


def _slow_steps(conn, project) -> list[dict]:
    rows = conn.execute(
        "SELECT name, duration_ms FROM ci_step "
        "WHERE project = ? AND duration_ms IS NOT NULL AND conclusion = 'success'",
        (project,),
    ).fetchall()
    buckets: dict[str, list[int]] = {}
    for r in rows:
        buckets.setdefault(r["name"], []).append(r["duration_ms"])
    ranked = sorted(
        ((n, _median(v), len(v)) for n, v in buckets.items() if len(v) >= 3),
        key=lambda t: -(t[1] or 0),
    )[:6]
    return [{"step": n, "median": ms(m), "samples": c} for n, m, c in ranked]


def _summarize_local(builds, recent, prior) -> dict:
    now_rows, then_rows = _bucket(builds, recent, prior, key="ts")
    out: dict = {"total_builds": len(builds)}

    for label, rows in (("recent", now_rows), ("previous", then_rows)):
        cc = [r for r in rows if r["config_cache"] in ("hit", "miss")]
        full = [r["duration_ms"] for r in rows
                if r["measured_from"] == "build_start" and r["duration_ms"] is not None]
        cached = [r["duration_ms"] for r in rows
                  if r["measured_from"] == "execution" and r["duration_ms"] is not None]
        out[label] = {
            "builds": len(rows),
            "config_cache_hit_rate_pct": _pct(sum(1 for r in cc if r["config_cache"] == "hit"), len(cc)),
            "median_full_build": ms(_median(full)),
            "median_cached_config_build": ms(_median(cached)),
            "failed_builds": sum(1 for r in rows if r["outcome"] == "failed"),
            "tasks_executed": sum(r["executed"] or 0 for r in rows),
            "tasks_from_cache": sum(r["from_cache"] or 0 for r in rows),
            "tasks_up_to_date": sum(r["up_to_date"] or 0 for r in rows),
        }

    buckets: dict[str, list[int]] = {}
    for b in builds:
        if b["duration_ms"] is not None:
            buckets.setdefault(b["tasks"], []).append(b["duration_ms"])
    out["task_sets"] = [
        {"tasks": t, "runs": len(v), "median": ms(_median(v))}
        for t, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:6]
    ]
    return out


# --- generation -------------------------------------------------------------

_PROMPT = """\
You are writing the analysis panel of a developer's private build-metrics
dashboard. Below is a JSON summary of one project. Every number in it was
computed from the database; you are interpreting, not calculating.

Write 2 to 4 short paragraphs of plain prose. Rules:

- Lead with whatever a developer would actually act on. If nothing changed
  meaningfully, say so plainly instead of manufacturing a finding.
- Quote figures from the JSON verbatim. Durations are already in human units
  (for example "24m", "8s") — use them exactly as written, never convert them.
  Never invent, extrapolate or round misleadingly.
- "recent" is the last {window} days, "previous" is the {window} days before it.
- Where a sample is too small to support a conclusion, say so. Small local-build
  counts are expected: collection started recently.
- Two duration figures are NOT comparable and must never be presented as a
  change: median_full_build measures configuration plus execution, while
  median_cached_config_build measures execution only, because on a
  configuration-cache hit Gradle exposes no earlier hook. Treat them as separate
  populations. The same applies to CI versus local builds.
- Plain paragraphs only. No markdown, no headings, no bullet lists, no preamble
  such as "Here is the analysis". Start with the first sentence of the analysis.
- Under 220 words total.

JSON summary:
{payload}
"""


def build_prompt(summary: dict) -> str:
    return _PROMPT.format(window=WINDOW_DAYS, payload=json.dumps(summary, indent=1, default=str))


def fingerprint(summary: dict) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]


def load_cached(conn: sqlite3.Connection, project: str) -> dict | None:
    raw = db.get_state(conn, STATE_PREFIX + project)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def generate(conn: sqlite3.Connection, project: str, *, force: bool = False,
             timeout: int = DEFAULT_TIMEOUT, log=lambda _m: None) -> dict | None:
    """Return {text, generated_at, fingerprint}, or None if unavailable.

    Never raises: a missing narrative is a smaller problem than a failed
    refresh.
    """
    summary = summarize(conn, project)
    if not summary.get("ci") and not summary.get("local"):
        return None

    print_ = log
    fp = fingerprint(summary)
    cached = load_cached(conn, project)
    if cached and cached.get("fingerprint") == fp and not force:
        print_("  analysis: unchanged since last run (cached)")
        return cached

    if not claude_cli.available():
        if cached:
            print_("  analysis: 'claude' not on PATH — keeping the previous one")
            return cached
        print_("  analysis: skipped ('claude' not on PATH)")
        return None

    print_("  analysis: asking claude…")
    try:
        text = claude_cli.run(build_prompt(summary), timeout)
    except claude_cli.ClaudeUnavailable as exc:
        print_(f"  analysis: skipped ({exc})")
        return cached
    record = {
        "text": text,
        "fingerprint": fp,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    db.set_state(conn, STATE_PREFIX + project, json.dumps(record))
    conn.commit()
    print_("  analysis: written")
    return record

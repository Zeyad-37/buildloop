"""Generate one self-contained ``dashboard-<project>.html`` per project.

One file per project rather than a project switcher: a selector is a filter UI,
and filter UIs are where "static file" starts becoming an application
(RFC T-055 §4.5).

CI and local sections never share an axis or a chart. CI is a cold,
clean-checkout hosted runner; local is a warm daemon on a laptop with a
populated cache. Plotting them together produces a comparison that looks
meaningful and is not.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import charts, db
from .charts import Chart, Series
from .humanize import count, ms, pct

JOB_WINDOW_DAYS = 90
TOP_N = 12


# --- aggregation helpers ----------------------------------------------------

def week_start(iso: str | None) -> str | None:
    """Monday of the week containing an ISO-8601 instant."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return (d - timedelta(days=d.weekday())).isoformat()


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. SQLite has no percentile function, and the
    data volume (thousands of rows) makes doing it in Python free."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p * (len(ordered) - 1))))
    return float(ordered[k])


def _weeks_between(first: str, last: str) -> list[str]:
    """Every week in range, including empty ones — gaps are information."""
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    out, cur = [], start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=7)
    return out


def _axis(rows_weeks: list[str]) -> list[str]:
    weeks = [w for w in rows_weeks if w]
    return _weeks_between(min(weeks), max(weeks)) if weeks else []


def _top_workflows(runs: list[sqlite3.Row], limit: int = 5) -> list[str]:
    tally: dict[str, int] = {}
    for r in runs:
        tally[r["workflow"]] = tally.get(r["workflow"], 0) + 1
    return [w for w, _ in sorted(tally.items(), key=lambda kv: -kv[1])[:limit]]


# --- CI charts --------------------------------------------------------------

def ci_duration_chart(cid, runs, weeks, workflows) -> Chart:
    series = []
    for wf in workflows:
        buckets: dict[str, list[float]] = {}
        for r in runs:
            if r["workflow"] != wf or r["exec_ms"] is None:
                continue
            wk = week_start(r["created_at"])
            if wk:
                buckets.setdefault(wk, []).append(r["exec_ms"])
        series.append(Series(f"{wf} (median)", [(w, percentile(v, 0.5)) for w, v in sorted(buckets.items())]))
    return charts.line_chart(
        cid, "CI run duration", "Median execution time per week, by workflow. Queue time excluded.",
        weeks, series, ms,
    )


def ci_p90_chart(cid, runs, weeks, workflows) -> Chart:
    series = []
    for wf in workflows:
        buckets: dict[str, list[float]] = {}
        for r in runs:
            if r["workflow"] != wf or r["exec_ms"] is None:
                continue
            wk = week_start(r["created_at"])
            if wk:
                buckets.setdefault(wk, []).append(r["exec_ms"])
        series.append(Series(f"{wf} (p90)", [(w, percentile(v, 0.9)) for w, v in sorted(buckets.items())]))
    return charts.line_chart(
        cid, "CI run duration — p90", "The tail, per week. A rising p90 with a flat median means variance, not slowdown.",
        weeks, series, ms,
    )


def ci_queue_vs_exec_chart(cid, runs, weeks) -> Chart:
    queue: dict[str, list[float]] = {}
    execu: dict[str, list[float]] = {}
    for r in runs:
        wk = week_start(r["created_at"])
        if not wk:
            continue
        if r["queue_ms"] is not None:
            queue.setdefault(wk, []).append(r["queue_ms"])
        if r["exec_ms"] is not None:
            execu.setdefault(wk, []).append(r["exec_ms"])
    return charts.stacked_bar_chart(
        cid, "Queue time vs execution time",
        "Median per week, stacked. A run that slowed because the runner queue was busy is a "
        "different problem from one whose build got slower.",
        weeks,
        [
            Series("queued", [(w, percentile(v, 0.5)) for w, v in sorted(queue.items())]),
            Series("executing", [(w, percentile(v, 0.5)) for w, v in sorted(execu.items())]),
        ],
        ms,
    )


def ci_failure_rate_chart(cid, runs, weeks, workflows) -> Chart:
    series = []
    for wf in workflows:
        tally: dict[str, list[int]] = {}
        for r in runs:
            if r["workflow"] != wf or r["conclusion"] is None:
                continue
            wk = week_start(r["created_at"])
            if not wk:
                continue
            bucket = tally.setdefault(wk, [0, 0])
            bucket[1] += 1
            if r["conclusion"] == "failure":
                bucket[0] += 1
        series.append(
            Series(wf, [(w, 100.0 * f / t) for w, (f, t) in sorted(tally.items()) if t])
        )
    return charts.line_chart(
        cid, "CI failure rate", "Percent of concluded runs that failed, per week, by workflow.",
        weeks, series, pct,
    )


def ci_flaky_jobs_chart(cid, conn, project) -> Chart:
    rows = conn.execute(
        """
        SELECT name,
               SUM(CASE WHEN conclusion = 'failure' THEN 1 ELSE 0 END) AS failures,
               COUNT(*) AS total
        FROM ci_job
        WHERE project = ? AND conclusion IS NOT NULL
        GROUP BY name
        HAVING failures > 0
        ORDER BY failures DESC
        LIMIT ?
        """,
        (project, TOP_N),
    ).fetchall()
    return charts.hbar_chart(
        cid, "Failures by job",
        f"Last {JOB_WINDOW_DAYS} days. Which job is the flaky one.",
        [(f"{r['name']}  ({r['failures']}/{r['total']})", r["failures"]) for r in rows],
        count,
        note=f"failures / runs, last {JOB_WINDOW_DAYS} days",
    )


def ci_slow_steps_chart(cid, conn, project) -> Chart:
    rows = conn.execute(
        """
        SELECT name, duration_ms FROM ci_step
        WHERE project = ? AND duration_ms IS NOT NULL AND conclusion = 'success'
        """,
        (project,),
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for r in rows:
        buckets.setdefault(r["name"], []).append(r["duration_ms"])
    ranked = sorted(
        ((n, percentile(v, 0.5) or 0) for n, v in buckets.items() if len(v) >= 3),
        key=lambda kv: -kv[1],
    )[:TOP_N]
    return charts.hbar_chart(
        cid, "Slowest steps",
        f"Median duration, last {JOB_WINDOW_DAYS} days, steps seen at least 3 times. Where inside the job.",
        ranked, ms, note="median step duration",
    )


FAILURE_GROUPS = 15


def ci_failure_reasons(conn: sqlite3.Connection, project: str, repo: str | None = None) -> str:
    """The table that says what the failures in "Failures by job" actually were."""
    since = (datetime.now(timezone.utc) - timedelta(days=JOB_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = db.failure_groups(conn, project, since)
    head = (
        '<section class="chart failures" id="failures"><figcaption>'
        "<h3>Why CI fails</h3>"
        f"<p>Failed jobs in the last {JOB_WINDOW_DAYS} days, grouped by the error in the failing "
        "step's log. The same failure with different numbers counts once. Expand a row for the "
        "latest occurrence.</p></figcaption>"
    )
    groups = data["groups"]
    if not groups:
        msg = ("No failed jobs in this window." if not data["total"]
               else "Failures are recorded but none have been diagnosed yet — run "
                    "<code>buildloop refresh</code>.")
        return f'{head}<p class="empty-note">{msg}</p></section>'

    items = []
    for g in groups[:FAILURE_GROUPS]:
        where = sorted(g["where"].items(), key=lambda kv: -kv[1])
        where_text = " · ".join(w for w, _ in where[:3])
        if len(where) > 3:
            where_text += f" · +{len(where) - 3} more"
        link = ""
        if repo:
            url = f"https://github.com/{repo}/actions/runs/{g['run_id']}/job/{g['job_id']}"
            link = f' · <a href="{charts.esc(url)}" rel="noreferrer">open job ↗</a>'
        excerpt = f"<pre>{charts.esc(g['excerpt'])}</pre>" if g["excerpt"] else ""
        items.append(
            f'<li><span class="n" title="failed jobs">{g["count"]:,}</span><div>'
            f'<details><summary>{charts.esc(g["reason"])}</summary>{excerpt}'
            f'<p class="muted">latest {charts.esc((g["last_seen"] or "")[:10])}{link}</p></details>'
            f'<p class="where">{charts.esc(where_text)}</p>'
            "</div></li>"
        )

    notes = []
    rest = groups[FAILURE_GROUPS:]
    if rest:
        notes.append(f"{len(rest)} rarer reasons ({sum(g['count'] for g in rest):,} failures) not shown")
    if data["knock_on"]:
        notes.append(f"{data['knock_on']:,} knock-on failures left out — jobs that failed only after "
                     "an earlier job in the same run had failed or been cancelled")
    if data["no_log"]:
        notes.append(f"{data['no_log']:,} with no log left on GitHub")
    if data["pending"]:
        notes.append(f"{data['pending']:,} not diagnosed yet")
    note_html = f'<p class="muted">{charts.esc("; ".join(notes))}.</p>' if notes else ""

    return f'{head}<ol class="reasons">{"".join(items)}</ol>{note_html}</section>'


# --- local build charts -----------------------------------------------------

def local_duration_chart(cid, builds, weeks) -> Chart:
    """Split by how the measurement was taken.

    A configuration-cache hit skips configuration entirely and buildloop can
    only observe from the first task event, so hit-builds and full builds are
    different populations — merging them into one line would show a "speedup"
    that is really a change in what was measured.
    """
    groups = {"build_start": {}, "execution": {}}
    for b in builds:
        wk = week_start(b["ts"])
        mode = b["measured_from"] if "measured_from" in b.keys() else None
        if wk and b["duration_ms"] is not None and mode in groups:
            groups[mode].setdefault(wk, []).append(b["duration_ms"])
    return charts.line_chart(
        cid, "Local build duration",
        "Median per week. Full builds cover configuration + execution; cached-config builds are "
        "execution only (Gradle exposes no earlier hook on a hit).",
        weeks,
        [
            Series("full build (config ran)", [(w, percentile(v, 0.5)) for w, v in sorted(groups["build_start"].items())]),
            Series("cached config", [(w, percentile(v, 0.5)) for w, v in sorted(groups["execution"].items())]),
        ],
        ms,
    )


def local_cache_chart(cid, builds, weeks) -> Chart:
    buckets: dict[str, list[int]] = {}
    for b in builds:
        wk = week_start(b["ts"])
        if not wk:
            continue
        acc = buckets.setdefault(wk, [0, 0, 0])
        acc[0] += b["from_cache"] or 0
        acc[1] += b["up_to_date"] or 0
        acc[2] += b["executed"] or 0
    ordered = sorted(buckets.items())
    return charts.stacked_bar_chart(
        cid, "Build cache effectiveness",
        "Tasks per week by outcome. A growing 'executed' band means the cache is earning less.",
        weeks,
        [
            Series("from cache", [(w, v[0]) for w, v in ordered]),
            Series("up to date", [(w, v[1]) for w, v in ordered]),
            Series("executed", [(w, v[2]) for w, v in ordered]),
        ],
        count,
    )


def local_config_cache_chart(cid, builds, weeks) -> Chart:
    tally: dict[str, list[int]] = {}
    for b in builds:
        wk = week_start(b["ts"])
        if not wk or b["config_cache"] not in ("hit", "miss"):
            continue
        acc = tally.setdefault(wk, [0, 0])
        acc[1] += 1
        if b["config_cache"] == "hit":
            acc[0] += 1
    return charts.line_chart(
        cid, "Configuration-cache hit rate",
        "Percent of builds that reused a cached configuration. Builds run with the cache disabled "
        "are excluded, not counted as misses.",
        weeks,
        [Series("hit rate", [(w, 100.0 * h / t) for w, (h, t) in sorted(tally.items()) if t])],
        pct,
    )


def local_task_set_chart(cid, builds) -> Chart:
    buckets: dict[str, list[float]] = {}
    for b in builds:
        if b["duration_ms"] is not None:
            buckets.setdefault(b["tasks"], []).append(b["duration_ms"])
    ranked = sorted(
        ((f"{t}  (n={len(v)})", percentile(v, 0.5) or 0) for t, v in buckets.items() if len(v) >= 3),
        key=lambda kv: -kv[1],
    )[:TOP_N]
    return charts.hbar_chart(
        cid, "Slowest task sets", "Median duration by invoked task set, at least 3 runs.",
        ranked, ms, note="median build duration",
    )


# --- page -------------------------------------------------------------------

def _analysis(record: dict | None) -> str:
    """Render the narrative, if one was produced.

    The text comes from a model, so it is escaped like any other untrusted
    string and rendered as plain paragraphs — never as markup.
    """
    if not record or not record.get("text"):
        return ""
    paragraphs = [p.strip() for p in record["text"].split("\n") if p.strip()]
    body = "".join(f"<p>{charts.esc(p)}</p>" for p in paragraphs)
    stamp = record.get("generated_at")
    by = f'<p class="by">Written by Claude from the computed summary{" · " + charts.esc(stamp) if stamp else ""}</p>'
    return f'<section class="analysis"><h2>What the numbers say</h2>{body}{by}</section>'


_SUGGESTIONS = (
    "Which CI job fails most often, and is it getting worse?",
    "Is CI slower this month than last month?",
    "Summarise my local builds this week.",
)


def _ask_panel(ask: dict | None) -> str:
    """The question box.

    Always rendered, so the feature is discoverable from the static file too —
    but only a page served by `buildloop serve` carries the token that makes
    it work. Everything Claude writes is inserted with `textContent`, never as
    markup.
    """
    live = bool(ask and ask.get("available"))
    if live:
        hint = "Answers come from the numbers behind these charts. Follow-up questions keep context."
    elif ask:
        hint = "The <code>claude</code> CLI isn't on PATH, so questions are unavailable."
    else:
        hint = ("Questions need the local server — run <code>buildloop serve</code> "
                "and open the page it prints.")
    disabled = "" if live else " disabled"
    chips = "".join(
        f'<button type="button" data-q="{charts.esc(q)}"{disabled}>{charts.esc(q)}</button>'
        for q in _SUGGESTIONS
    )
    return (
        '<section class="ask" id="ask" aria-labelledby="ask-h">'
        '<h2 id="ask-h">Ask Claude about this data</h2>'
        f'<p class="hint">{hint}</p>'
        '<div class="log" id="ask-log" aria-live="polite"></div>'
        '<form id="ask-form">'
        f'<input id="ask-q" name="q" maxlength="500" autocomplete="off" aria-label="Question" '
        f'placeholder="e.g. Why did Verify PRs get flakier?"{disabled}>'
        f'<button type="submit"{disabled}>Ask</button>'
        '</form>'
        f'<div class="chips">{chips}</div>'
        '</section>'
    )


def _stat(label: str, value: str, note: str = "") -> str:
    note_html = f"<small>{charts.esc(note)}</small>" if note else ""
    return f'<div class="stat"><dt>{charts.esc(label)}</dt><dd>{charts.esc(value)}{note_html}</dd></div>'


def _summary(runs, builds) -> str:
    recent = [r for r in runs if r["exec_ms"] is not None][-200:]
    concluded = [r for r in runs if r["conclusion"] is not None][-200:]
    failures = sum(1 for r in concluded if r["conclusion"] == "failure")
    hits = [b for b in builds if b["config_cache"] in ("hit", "miss")]
    hit_rate = 100.0 * sum(1 for b in hits if b["config_cache"] == "hit") / len(hits) if hits else None

    cells = [
        _stat("CI runs stored", f"{len(runs):,}"),
        _stat("Median CI run", ms(percentile([r["exec_ms"] for r in recent], 0.5)) if recent else "—", "last 200"),
        _stat("CI failure rate", pct(100.0 * failures / len(concluded)) if concluded else "—", "last 200"),
        _stat("Local builds stored", f"{len(builds):,}"),
        _stat("Config-cache hits", pct(hit_rate) if hit_rate is not None else "—", "all time"),
    ]
    return f'<dl class="stats">{"".join(cells)}</dl>'


_CSS = """
:root{color-scheme:light dark;
  --bg:#fbfbfa;--fg:#1a1a1a;--muted:#666;--line:#e2e2df;--card:#fff;
  --s0:#3b6ea5;--s1:#c1663a;--s2:#4c8b6a;--s3:#8a5fa8;--s4:#b3893c;--s5:#a44a5e;}
@media (prefers-color-scheme:dark){:root{
  --bg:#16181a;--fg:#e8e8e6;--muted:#9a9a97;--line:#2c2f33;--card:#1d2023;
  --s0:#6fa8dc;--s1:#e0895e;--s2:#7dc3a0;--s3:#b48ed0;--s4:#d8b160;--s5:#d4788c;}}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:var(--bg);color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:960px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:19px;margin:44px 0 6px;padding-top:22px;border-top:1px solid var(--line)}
h2:first-of-type{border-top:0}
h3{font-size:15px;margin:0}
.sub{color:var(--muted);margin:0 0 8px}
.section-note{color:var(--muted);margin:0 0 18px;max-width:70ch}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 8px;padding:0}
.stat{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.stat dt{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.stat dd{margin:2px 0 0;font-size:21px;font-variant-numeric:tabular-nums}
.stat small{display:block;font-size:11px;color:var(--muted);font-weight:400}
.chart{margin:20px 0;padding:14px 16px 6px;background:var(--card);
  border:1px solid var(--line);border-radius:8px;overflow-x:auto}
figcaption p{margin:2px 0 0;color:var(--muted);font-size:13px;max-width:72ch}
svg{display:block;width:100%;height:auto;margin-top:10px;min-width:640px}
.grid{stroke:var(--line);stroke-width:1}
.tick,.rowlabel{fill:var(--muted);font-size:11px;font-family:inherit}
.rowlabel{font-size:12px;fill:var(--fg)}
.empty{fill:var(--muted);font-size:13px;font-family:inherit}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:9px;font-size:12px;color:var(--muted)}
.hit{fill:transparent;cursor:crosshair}
.dot{opacity:0;transition:opacity .08s;pointer-events:none}
.chart.active .dot[data-on]{opacity:1}
.cursor{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:0;pointer-events:none}
.chart.active .cursor{opacity:.6}
#tip{position:fixed;z-index:9;pointer-events:none;opacity:0;transition:opacity .08s;
  background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:7px;
  padding:8px 10px;font-size:12px;line-height:1.45;box-shadow:0 4px 16px rgba(0,0,0,.16);
  max-width:320px}
#tip.on{opacity:1}
#tip b{display:block;margin-bottom:4px;font-size:11px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.04em}
#tip .r{display:flex;align-items:center;gap:7px;white-space:nowrap}
#tip .r i{width:9px;height:9px;border-radius:2px;flex:none}
#tip .r span{flex:1;overflow:hidden;text-overflow:ellipsis}
#tip .r em{font-style:normal;font-variant-numeric:tabular-nums;font-weight:600}
#tip .tot{margin-top:4px;padding-top:4px;border-top:1px solid var(--line)}
.analysis{margin:22px 0 6px;padding:16px 18px;background:var(--card);
  border:1px solid var(--line);border-left:3px solid var(--s0);border-radius:8px}
.analysis h2{margin:0 0 8px;padding:0;border:0;font-size:14px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted)}
.analysis p{margin:0 0 10px;max-width:74ch}
.analysis p:last-of-type{margin-bottom:0}
.analysis .by{margin-top:10px;font-size:11px;color:var(--muted)}
.ask{margin:14px 0 6px;padding:16px 18px;background:var(--card);border:1px solid var(--line);border-radius:8px}
.ask h2{margin:0 0 4px;padding:0;border:0;font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.ask .hint{margin:0 0 10px;font-size:13px;color:var(--muted)}
.ask code{font-size:12px;padding:1px 5px;border-radius:4px;background:var(--line)}
.ask form{display:flex;gap:8px}
.ask input{flex:1;min-width:0;font:inherit;padding:8px 10px;border-radius:6px;
  border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.ask input:focus{outline:2px solid var(--s0);outline-offset:1px}
.ask button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--s0);
  background:var(--s0);color:#fff;cursor:pointer}
.ask button:disabled,.ask input:disabled{opacity:.5;cursor:not-allowed}
.ask .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.ask .chips button{font-size:12px;padding:4px 10px;background:transparent;color:var(--fg);border-color:var(--line)}
.ask .log{display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
.ask .log:empty{display:none}
.ask .q{align-self:flex-end;max-width:85%;padding:7px 11px;border-radius:10px 10px 2px 10px;
  background:var(--s0);color:#fff;white-space:pre-wrap}
.ask .a{max-width:95%;padding:9px 12px;border-radius:10px 10px 10px 2px;border:1px solid var(--line);
  white-space:pre-wrap;line-height:1.5}
.ask .a.wait{color:var(--muted);font-style:italic}
.ask .a.err{border-color:var(--s5);color:var(--s5)}
.reasons{list-style:none;margin:12px 0 8px;padding:0;font-size:13px}
.reasons li{display:grid;grid-template-columns:3.2em minmax(0,1fr);gap:10px;padding:9px 0;
  border-top:1px solid var(--line)}
.reasons .n{text-align:right;font-variant-numeric:tabular-nums;font-size:15px;font-weight:600;line-height:1.35}
.reasons .where{margin:3px 0 0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.reasons summary{overflow-wrap:anywhere}
.failures summary{cursor:pointer}
.failures pre{margin:8px 0 4px;padding:8px 10px;background:var(--bg);border:1px solid var(--line);
  border-radius:6px;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}
.failures a{color:var(--s0)}
.muted{color:var(--muted);font-size:12px}
.failures .muted{margin:4px 0}
.failures code{font-size:12px;padding:1px 5px;border-radius:4px;background:var(--line)}
.empty-note{color:var(--muted);font-size:13px}
.key{display:inline-flex;align-items:center;gap:6px}
.key i{width:11px;height:11px;border-radius:2px;display:inline-block}
footer{margin-top:44px;color:var(--muted);font-size:12px}
"""


_TOOLTIP_JS = """
(function () {
  var DATA = window.BUILDLOOP || {};
  var tip = document.getElementById('tip');
  if (!tip) return;

  function swatch(i) {
    return '<i style="background:var(--s' + (i % 6) + ')"></i>';
  }

  // Values are formatted in Python, so the browser only escapes and places
  // strings. Keeping one set of formatters avoids the two drifting apart.
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function rows(meta, i) {
    var out = '';
    if (meta.type === 'hbar') {
      var r = meta.rows[i];
      if (!r) return '';
      return '<b>' + esc(meta.note || 'value') + '</b><div class="r">' + swatch(0) +
             '<span>' + esc(r.label) + '</span><em>' + esc(r.value) + '</em></div>';
    }
    out += '<b>week of ' + esc(meta.categories[i]) + '</b>';
    var shown = 0;
    meta.series.forEach(function (s, si) {
      var v = s.values[i];
      if (v === null || v === undefined) return;
      shown++;
      out += '<div class="r">' + swatch(si) + '<span>' + esc(s.label) +
             '</span><em>' + esc(v) + '</em></div>';
    });
    if (!shown) out += '<div class="r"><span>no data</span></div>';
    if (meta.type === 'stack' && meta.totals && shown > 1) {
      out += '<div class="r tot"><span>total</span><em>' + esc(meta.totals[i]) + '</em></div>';
    }
    return out;
  }

  function place(evt) {
    var pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    var x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = Math.max(8, y) + 'px';
  }

  function hide(fig) {
    if (fig) {
      fig.classList.remove('active');
      fig.querySelectorAll('.dot[data-on]').forEach(function (d) { d.removeAttribute('data-on'); });
    }
    tip.classList.remove('on');
  }

  Object.keys(DATA).forEach(function (id) {
    var fig = document.getElementById(id);
    if (!fig) return;
    var meta = DATA[id];
    var svg = fig.querySelector('svg');
    var cursor = fig.querySelector('.cursor');

    fig.querySelectorAll('.hit').forEach(function (hit) {
      var i = +hit.getAttribute('data-i');

      hit.addEventListener('mousemove', function (evt) {
        var html = rows(meta, i);
        if (!html) { hide(fig); return; }
        tip.innerHTML = html;
        tip.classList.add('on');
        place(evt);
        fig.classList.add('active');
        fig.querySelectorAll('.dot[data-on]').forEach(function (d) { d.removeAttribute('data-on'); });
        fig.querySelectorAll('.dot[data-i="' + i + '"]').forEach(function (d) {
          d.setAttribute('data-on', '');
        });
        if (cursor) {
          var cx = +hit.getAttribute('x') + (+hit.getAttribute('width')) / 2;
          cursor.setAttribute('x1', cx);
          cursor.setAttribute('x2', cx);
        }
      });
    });

    svg.addEventListener('mouseleave', function () { hide(fig); });
  });

  window.addEventListener('scroll', function () { hide(document.querySelector('.chart.active')); }, true);
})();
"""


_ASK_JS = """
(function () {
  var cfg = window.BUILDLOOP_ASK;
  var form = document.getElementById('ask-form');
  if (!cfg || !form) return;
  var input = document.getElementById('ask-q');
  var log = document.getElementById('ask-log');
  var buttons = document.querySelectorAll('#ask button');
  var history = [];
  var busy = false;

  function bubble(cls, text) {
    var el = document.createElement('div');
    el.className = cls;
    el.textContent = text;          // never innerHTML: this text came from a model
    log.appendChild(el);
    return el;
  }

  function setBusy(on) {
    busy = on;
    input.disabled = on;
    buttons.forEach(function (b) { b.disabled = on; });
  }

  function ask(question) {
    question = question.trim();
    if (!question || busy) return;
    bubble('q', question);
    var pending = bubble('a wait', 'Thinking…');
    setBusy(true);
    input.value = '';

    fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Buildloop-Token': cfg.token },
      body: JSON.stringify({ project: cfg.project, question: question, history: history.slice(-6) })
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (r) {
        pending.classList.remove('wait');
        if (r.ok && r.body.answer) {
          pending.textContent = r.body.answer;
          history.push({ question: question, answer: r.body.answer });
        } else {
          pending.classList.add('err');
          pending.textContent = r.body.error || 'Something went wrong.';
        }
      })
      .catch(function () {
        pending.classList.remove('wait');
        pending.classList.add('err');
        pending.textContent = "Couldn't reach buildloop serve — is it still running?";
      })
      .then(function () {
        setBusy(false);
        input.focus();
      });
  }

  form.addEventListener('submit', function (e) { e.preventDefault(); ask(input.value); });
  document.querySelectorAll('#ask .chips button').forEach(function (b) {
    b.addEventListener('click', function () { ask(b.getAttribute('data-q')); });
  });
})();
"""


def render(conn: sqlite3.Connection, project: str, analysis: dict | None = None,
           ask: dict | None = None, repo: str | None = None) -> str:
    runs = conn.execute(
        "SELECT * FROM ci_run WHERE project = ? ORDER BY created_at", (project,)
    ).fetchall()
    builds = conn.execute(
        "SELECT * FROM gradle_build WHERE project = ? ORDER BY ts", (project,)
    ).fetchall()

    ci_weeks = _axis([week_start(r["created_at"]) for r in runs])
    local_weeks = _axis([week_start(b["ts"]) for b in builds])
    workflows = _top_workflows(runs)

    ci_charts = [
        ci_duration_chart("c1", runs, ci_weeks, workflows),
        ci_p90_chart("c2", runs, ci_weeks, workflows),
        ci_queue_vs_exec_chart("c3", runs, ci_weeks),
        ci_failure_rate_chart("c4", runs, ci_weeks, workflows),
        ci_flaky_jobs_chart("c5", conn, project),
        ci_slow_steps_chart("c6", conn, project),
    ]
    local_charts = [
        local_duration_chart("c7", builds, local_weeks),
        local_task_set_chart("c8", builds),
        local_cache_chart("c9", builds, local_weeks),
        local_config_cache_chart("c10", builds, local_weeks),
    ]
    # The reasons table sits right under "Failures by job": the chart says
    # which job, the table says what went wrong in it.
    ci_section = (
        "".join(c.html for c in ci_charts[:5])
        + ci_failure_reasons(conn, project, repo)
        + "".join(c.html for c in ci_charts[5:])
    )
    local_section = "".join(c.html for c in local_charts)

    ids = [f"c{i}" for i in range(1, len(ci_charts) + len(local_charts) + 1)]
    registry = {
        cid: c.meta
        for cid, c in zip(ids, ci_charts + local_charts)
        if c.meta is not None
    }
    # </ ends a script element wherever it appears inside one, including inside
    # a string literal, so it has to be broken up.
    blob = json.dumps(registry, separators=(",", ":")).replace("</", "<\\/")
    ask_cfg = (
        json.dumps({"project": ask["project"], "token": ask["token"]}).replace("</", "<\\/")
        if ask and ask.get("available") else "null"
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>buildloop — {charts.esc(project)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{charts.esc(project)}</h1>
<p class="sub">Is the development loop getting faster or slower, and is CI getting flakier?</p>
{_summary(runs, builds)}
{_analysis(analysis)}
{_ask_panel(ask)}

<h2>GitHub Actions</h2>
<p class="section-note">Cold, clean-checkout hosted runners.</p>
{ci_section}

<h2>Local Gradle builds</h2>
<p class="section-note">A warm daemon on this machine with a populated cache. Deliberately kept on
separate axes from CI above — they measure different populations, and plotting them together
produces a comparison that looks meaningful and is not.</p>
{local_section}

<footer>Generated {charts.esc(generated)} by buildloop · {len(runs):,} CI runs · {len(builds):,} local builds</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>window.BUILDLOOP={blob};</script>
<script>{_TOOLTIP_JS}</script>
<script>window.BUILDLOOP_ASK={ask_cfg};</script>
<script>{_ASK_JS}</script>
</body></html>
"""

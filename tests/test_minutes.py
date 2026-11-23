import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401

from buildloop import ask, charts, dashboard, db, insights, minutes
from buildloop.humanize import mins, mins_axis


def days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRunnerOs(unittest.TestCase):
    def test_hosted_labels(self):
        self.assertEqual(minutes.runner_os("ubuntu-latest"), minutes.LINUX)
        self.assertEqual(minutes.runner_os("ubuntu-24.04-arm"), minutes.LINUX)
        self.assertEqual(minutes.runner_os("windows-2022"), minutes.WINDOWS)
        self.assertEqual(minutes.runner_os("macos-latest"), minutes.MACOS)

    def test_self_hosted_wins_over_its_os_label(self):
        # It carries "linux" too, but it is not on GitHub's bill.
        self.assertEqual(minutes.runner_os("self-hosted,linux,x64"), minutes.SELF_HOSTED)

    def test_unrecognised_is_other_not_linux(self):
        # A job stored before labels existed keeps the runner name instead.
        self.assertEqual(minutes.runner_os("GitHub Actions 5"), minutes.OTHER)
        self.assertEqual(minutes.runner_os(None), minutes.OTHER)
        self.assertEqual(minutes.runner_os(""), minutes.OTHER)


class TestBilled(unittest.TestCase):
    def test_each_job_rounds_up_to_the_whole_minute(self):
        self.assertEqual(minutes.billed(5_000), 1)
        self.assertEqual(minutes.billed(60_000), 1)
        self.assertEqual(minutes.billed(60_001), 2)

    def test_a_job_that_never_ran_costs_nothing(self):
        self.assertEqual(minutes.billed(0), 0)
        self.assertEqual(minutes.billed(None), 0)

    def test_formatter_stays_in_minutes(self):
        # The quota is stated in minutes; "20.6h" would need converting back.
        self.assertEqual(mins(1234), "1,234 min")
        self.assertEqual(mins(None), "—")

    def test_axis_ticks_fit_the_gutter(self):
        # "10,000 min" is wider than the axis gutter and was clipped to "0,000 min".
        self.assertEqual(mins_axis(10_000), "10k min")
        self.assertEqual(mins_axis(7_500), "7.5k min")
        self.assertEqual(mins_axis(250), "250 min")
        # Quarter ticks of a nice max like 25,000 land on 18,750.
        self.assertEqual(mins_axis(18_750), "18.8k min")
        self.assertEqual(mins_axis(1_875), "1.88k min")
        self.assertEqual(mins_axis(1_875_000), "1.88M min")

    def test_every_tick_of_a_nice_axis_fits(self):
        for v in (1_000, 2_000, 2_500, 5_000, 10_000, 20_000, 25_000, 50_000, 100_000, 250_000):
            m = charts._nice_max(v)
            for i in range(5):
                tick = mins_axis(m * (1 - i / 4))
                self.assertLessEqual(len(tick), 9, f"{tick!r} on an axis to {m}")


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")
        self.next_id = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_run(self, days, workflow="Verify PRs", jobs=()):
        """``jobs`` is a list of (duration_ms, runner)."""
        self.next_id += 1
        run_id, ts = self.next_id, days_ago(days)
        db.upsert_run(self.conn, {
            "run_id": run_id, "project": "p", "workflow": workflow, "branch": "b",
            "event": "push", "status": "completed", "conclusion": "success", "attempt": 1,
            "created_at": ts, "started_at": ts, "updated_at": ts,
            "queue_ms": 1_000, "exec_ms": max((d or 0 for d, _ in jobs), default=0),
        })
        for duration, runner in jobs:
            self.next_id += 1
            db.upsert_job(self.conn, {
                "job_id": self.next_id, "run_id": run_id, "project": "p", "name": "j",
                "conclusion": "success" if duration else "skipped",
                "started_at": ts, "completed_at": ts, "duration_ms": duration, "runner": runner,
            })


MINI = "self-hosted,macOS,ARM64"


class TestWhere(_Db):
    def test_only_the_self_hosted_label_makes_a_runner_self_hosted(self):
        self.assertEqual(minutes.runner_where(MINI), minutes.SELF_HOSTED)
        self.assertEqual(minutes.runner_where("macos-latest"), minutes.HOSTED)
        self.assertEqual(minutes.runner_where("blacksmith-4vcpu-ubuntu-2404"), minutes.HOSTED)

    def test_sql_and_python_agree_on_self_hosted(self):
        # Queries filter with db.SELF_HOSTED_SQL, everything else with runner_where.
        for label in (MINI, " Self-Hosted , x64", "self-hosted\t,linux", "self-hosted\r\n",
                      "ubuntu-latest", "blacksmith-4vcpu-ubuntu-2404", "", None):
            (in_sql,), = self.conn.execute(
                f"SELECT {db.SELF_HOSTED_SQL} FROM (SELECT ? AS runner) j", (label,))
            self.assertEqual(bool(in_sql), minutes.runner_where(label) == minutes.SELF_HOSTED,
                             repr(label))

    def test_a_run_split_between_the_two_is_mixed(self):
        self.add_run(3, jobs=[(60_000, "ubuntu-latest")])
        self.add_run(2, jobs=[(60_000, MINI), (60_000, MINI)])
        self.add_run(1, jobs=[(60_000, MINI), (60_000, "ubuntu-latest")])
        where = [w for _, w in minutes.runs_where(minutes.jobs(self.conn, "p")).values()]
        self.assertEqual(where, [minutes.HOSTED, minutes.SELF_HOSTED, minutes.MIXED])

    def test_a_skipped_job_does_not_decide_where_a_run_ran(self):
        # It carries the label it asked for, but never got a runner.
        self.add_run(1, jobs=[(60_000, "ubuntu-latest"), (None, MINI)])
        (_, where), = minutes.runs_where(minutes.jobs(self.conn, "p")).values()
        self.assertEqual(where, minutes.HOSTED)

    def test_totals_split_minutes_by_where(self):
        self.add_run(1, jobs=[(120_000, "ubuntu-latest"), (60_000, MINI)])
        t = minutes.totals(minutes.jobs(self.conn, "p"))
        self.assertEqual(t["by_where"], {minutes.HOSTED: 2, minutes.SELF_HOSTED: 1})

    def test_chart_counts_runs_not_jobs(self):
        self.add_run(1, jobs=[(60_000, MINI), (60_000, MINI)])
        self.add_run(1, jobs=[(60_000, "ubuntu-latest")])
        meta = dashboard.ci_where_chart("c", minutes.jobs(self.conn, "p")).meta
        self.assertEqual([(s["label"], s["values"]) for s in meta["series"]],
                         [(minutes.HOSTED, ["1"]), (minutes.SELF_HOSTED, ["1"])])

    def test_comparison_needs_three_successes_on_each(self):
        for _ in range(3):
            self.add_run(1, jobs=[(600_000, "ubuntu-latest"), (200_000, MINI)])
        rows = dashboard.runner_comparison(minutes.jobs(self.conn, "p"))
        self.assertEqual([(r["job"], r["hosted"], r["self_hosted"]) for r in rows],
                         [("j", 600_000, 200_000)])
        self.assertIn("3.0× faster", dashboard.ci_runner_comparison(minutes.jobs(self.conn, "p")))
        self.conn.execute("DELETE FROM ci_job WHERE job_id = (SELECT MAX(job_id) FROM ci_job)")
        self.assertEqual(dashboard.runner_comparison(minutes.jobs(self.conn, "p")), [])

    def test_page_shows_the_views_only_once_self_hosted_was_used(self):
        self.add_run(1, jobs=[(60_000, "ubuntu-latest")])
        self.assertNotIn("Where CI ran", dashboard.render(self.conn, "p"))
        self.add_run(1, jobs=[(60_000, MINI)])
        html = dashboard.render(self.conn, "p")
        self.assertIn("Where CI ran", html)
        self.assertIn("Same job, hosted vs self-hosted", html)
        self.assertIn("1 min self-hosted", html)

    def view(self, runner):
        page = dashboard.render(self.conn, "p")
        return page.split(f'data-runner="{runner}"')[1].split('<div class="view"')[0]

    def test_no_switch_and_no_views_without_a_self_hosted_job(self):
        self.add_run(1, jobs=[(60_000, "ubuntu-latest")])
        page = dashboard.render(self.conn, "p")
        self.assertNotIn('<fieldset', page)
        self.assertNotIn('class="view"', page)

    def test_each_runner_view_counts_only_its_own_minutes(self):
        self.add_run(2, jobs=[(120_000, "ubuntu-latest")])
        self.add_run(1, jobs=[(60_000, MINI)])
        tile = "<dt>CI minutes</dt><dd>"
        self.assertIn(tile + "3 min", self.view("all"))
        self.assertIn(tile + "2 min<small>last 28 days</small>", self.view("hosted"))
        self.assertIn(tile + "1 min", self.view("self"))

    def test_only_the_combined_view_shows_at_first(self):
        self.add_run(1, jobs=[(60_000, MINI)])
        page = dashboard.render(self.conn, "p")
        self.assertIn('data-runner="all">', page)
        self.assertIn('data-runner="hosted" hidden>', page)
        self.assertIn('data-runner="self" hidden>', page)

    def test_runs_split_by_where_they_ran(self):
        # Median run: 10m on hosted, 2m on the mini, and the older of the two across both.
        self.add_run(3, jobs=[(600_000, "ubuntu-latest")])
        self.add_run(2, jobs=[(120_000, MINI)])
        self.add_run(1, jobs=[(120_000, MINI), (300_000, "ubuntu-latest")])   # mixed: All only
        tile = "<dt>Median CI run</dt><dd>"
        self.assertIn(tile + "5m", self.view("all"))
        self.assertIn(tile + "10m", self.view("hosted"))
        self.assertIn(tile + "2m", self.view("self"))

    def test_a_run_with_no_job_detail_counts_as_hosted(self):
        self.add_run(200, jobs=[])
        self.add_run(1, jobs=[(60_000, MINI)])
        self.assertIn("<dt>Median CI run</dt><dd>0s", self.view("hosted"))

    def test_failures_by_job_follow_the_runner(self):
        self.add_run(1, jobs=[(60_000, MINI), (60_000, "ubuntu-latest")])
        self.conn.execute("UPDATE ci_job SET conclusion = 'failure' WHERE runner = ?", (MINI,))
        chart = dashboard.ci_flaky_jobs_chart
        self.assertIn("(1/2)", chart("c", self.conn, "p").html)
        self.assertIn("(1/1)", chart("c", self.conn, "p", where=minutes.SELF_HOSTED).html)
        self.assertNotIn("(1/", chart("c", self.conn, "p", where=minutes.HOSTED).html)

    def test_failure_reasons_follow_the_runner(self):
        self.add_run(1, jobs=[(60_000, MINI)])
        self.conn.execute("UPDATE ci_job SET conclusion = 'failure'")
        job_id = self.conn.execute("SELECT job_id FROM ci_job").fetchone()[0]
        db.upsert_failure(self.conn, {"job_id": job_id, "project": "p", "step": "s",
                                      "reason": "boom", "signature": "boom", "excerpt": ""})
        groups = lambda where: db.failure_groups(self.conn, "p", days_ago(90), where=where)
        self.assertEqual(groups(minutes.SELF_HOSTED)["total"], 1)
        self.assertEqual(groups(minutes.HOSTED)["total"], 0)


class TestTotals(_Db):
    def test_parallel_jobs_add_up_and_round_separately(self):
        # Wall-clock is 10 minutes; the bill is 10 + 1 + 1.
        self.add_run(1, jobs=[(600_000, "ubuntu-latest"), (5_000, "ubuntu-latest"),
                              (30_000, "macos-latest")])
        t = minutes.totals(minutes.jobs(self.conn, "p"))
        self.assertEqual(t["total"], 12)
        self.assertEqual(t["by_os"], {minutes.LINUX: 11, minutes.MACOS: 1})
        self.assertEqual(t["by_workflow"], {"Verify PRs": 12})

    def test_skipped_jobs_are_not_rows(self):
        self.add_run(1, jobs=[(0, None), (None, None), (60_000, "ubuntu-latest")])
        self.assertEqual(len(minutes.jobs(self.conn, "p")), 1)

    def test_since_bounds_the_rows(self):
        self.add_run(40, jobs=[(60_000, "ubuntu-latest")])
        self.add_run(1, jobs=[(60_000, "ubuntu-latest")])
        self.assertEqual(len(minutes.jobs(self.conn, "p", days_ago(28))), 1)


class TestChart(_Db):
    def test_first_week_is_dropped_as_partial(self):
        for weeks_back in (0, 1, 2):
            self.add_run(7 * weeks_back + 0.01, jobs=[(60_000, "ubuntu-latest")])
        meta = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).meta
        first = dashboard.week_start(days_ago(14.01))
        self.assertNotIn(first, meta["categories"])
        self.assertEqual(len(meta["categories"]), 2)

    def test_a_single_week_is_kept(self):
        self.add_run(0.01, jobs=[(60_000, "ubuntu-latest")])
        meta = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).meta
        self.assertEqual(len(meta["categories"]), 1)

    def test_only_runner_kinds_that_were_used_get_a_series(self):
        self.add_run(0.01, jobs=[(60_000, "macos-latest")])
        meta = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).meta
        self.assertEqual([s["label"] for s in meta["series"]], [minutes.MACOS])

    def test_an_os_only_in_the_dropped_week_gets_no_series(self):
        # An empty legend key: macOS ran only in the partial first week.
        self.add_run(14.01, jobs=[(60_000, "macos-latest")])
        self.add_run(7.01, jobs=[(60_000, "ubuntu-latest")])
        self.add_run(0.01, jobs=[(60_000, "ubuntu-latest")])
        meta = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).meta
        self.assertEqual([s["label"] for s in meta["series"]], [minutes.LINUX])

    def test_still_running_only_when_the_last_bar_is_this_week(self):
        self.add_run(15, jobs=[(60_000, "ubuntu-latest")])
        self.add_run(8, jobs=[(60_000, "ubuntu-latest")])
        html = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).html
        self.assertNotIn("still running", html)

        # Seconds ago, not minutes: it must land in this week even just past midnight on Monday.
        self.add_run(7.0001, jobs=[(60_000, "ubuntu-latest")])
        self.add_run(0.0001, jobs=[(60_000, "ubuntu-latest")])
        html = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).html
        self.assertIn("still running", html)

    def test_stat_counts_the_last_28_days(self):
        self.add_run(40, jobs=[(600_000, "ubuntu-latest")])
        self.add_run(1, jobs=[(120_000, "ubuntu-latest")])
        html = dashboard.render(self.conn, "p")
        self.assertIn("CI minutes</dt><dd>2 min", html)


class TestSummary(_Db):
    def test_recent_and_previous_minutes(self):
        self.add_run(3, jobs=[(600_000, "ubuntu-latest"), (60_000, "macos-latest")])
        self.add_run(40, jobs=[(120_000, "ubuntu-latest")], workflow="Nightly")
        m = insights.summarize(self.conn, "p")["ci"]["minutes"]
        self.assertEqual(m["recent"]["total"], "11 min")
        self.assertEqual(m["recent"]["by_runner_os"], {"Linux": "10 min", "macOS": "1 min"})
        self.assertEqual(m["previous"]["top_workflows"], {"Nightly": "2 min"})

    def test_no_job_detail_means_no_minutes_section(self):
        # Zero would be a claim; missing data is not a quiet month.
        self.add_run(3)
        self.assertIsNone(insights.summarize(self.conn, "p")["ci"]["minutes"])

    def test_ask_context_has_weekly_minutes(self):
        for weeks_back in (0, 1, 2):
            self.add_run(7 * weeks_back + 0.01, jobs=[(60_000, "ubuntu-latest")])
        weekly = ask.build_context(self.conn, "p")["ci_minutes_weekly"]
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[-1]["total"], "1 min")

    def test_ask_context_has_the_charts_weeks_gaps_included(self):
        # A zero-minute week must be visible, or "fewest minutes?" is answered wrongly.
        for weeks_back in (0, 2, 3):
            self.add_run(7 * weeks_back + 0.01, jobs=[(60_000, "ubuntu-latest")])
        weekly = ask.build_context(self.conn, "p")["ci_minutes_weekly"]
        meta = dashboard.ci_minutes_chart("c", minutes.jobs(self.conn, "p")).meta
        self.assertEqual([w["week_of"] for w in weekly], meta["categories"])
        self.assertEqual(len(weekly), 3)
        self.assertEqual(weekly[1]["total"], "0 min")
        self.assertEqual(weekly[1]["by_runner_os"], {})


if __name__ == "__main__":
    unittest.main()

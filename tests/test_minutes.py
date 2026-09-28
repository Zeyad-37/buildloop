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


if __name__ == "__main__":
    unittest.main()

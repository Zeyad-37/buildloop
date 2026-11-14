import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401

from buildloop import ask, db, trends

MIN = 60_000
HOUR = 60 * MIN


def iso(d: date, hour: int = 12) -> str:
    return f"{d.isoformat()}T{hour:02d}:00:00Z"


def monday(weeks_ago: int) -> date:
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)


class TestWindow(unittest.TestCase):
    def test_window_starts_on_a_monday_covering_whole_weeks(self):
        start = trends.window_start(date(2026, 11, 13), weeks=26)  # a Friday
        self.assertEqual(start, "2026-05-18")  # this Monday (11-09) and 25 before it
        self.assertEqual(date.fromisoformat(start).weekday(), 0)

    def test_task_retention_covers_the_window_on_every_weekday(self):
        self.assertGreaterEqual(db.GRADLE_TASK_DAYS, trends.WEEKS * 7 + 7)
        for offset in range(7):
            d = date(2026, 11, 9) + timedelta(days=offset)  # Monday through Sunday
            end_of_day = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
            cutoff = end_of_day - timedelta(days=db.GRADLE_TASK_DAYS)
            start = datetime.fromisoformat(trends.window_start(d)).replace(tzinfo=timezone.utc)
            self.assertLessEqual(cutoff, start, d.isoformat())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")
        self.ids = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def next_id(self):
        self.ids += 1
        return self.ids


class CiJobs:
    """Helper for tests that need CI runs; holds no tests of its own."""

    def add_job(self, started, steps, workflow="Verify PRs", job="build", started_at=True):
        run_id, job_id = self.next_id(), self.next_id()
        db.upsert_run(self.conn, {
            "run_id": run_id, "project": "p", "workflow": workflow, "branch": "b",
            "event": "push", "status": "completed", "conclusion": "success", "attempt": 1,
            "created_at": started, "started_at": started, "updated_at": started,
            "queue_ms": 0, "exec_ms": 0,
        })
        db.upsert_job(self.conn, {
            "job_id": job_id, "run_id": run_id, "project": "p", "name": job,
            "conclusion": "success", "started_at": started if started_at else None,
            "completed_at": started, "duration_ms": 0, "runner": "ubuntu-latest",
        })
        for number, (name, conclusion, duration) in enumerate(steps, 1):
            db.upsert_step(self.conn, {"job_id": job_id, "number": number, "project": "p",
                                       "name": name, "conclusion": conclusion,
                                       "duration_ms": duration})


class TestCiStepWeekly(CiJobs, Base):
    def test_weekly_median_p90_and_samples_per_step(self):
        wk = monday(2)
        for minutes in (1, 2, 3, 4, 10):
            self.add_job(iso(wk), [("Test", "success", minutes * MIN)])
        self.add_job(iso(monday(1)), [("Test", "success", 5 * MIN)])

        out = trends.ci_step_weekly(self.conn, "p")
        step = out["steps"][0]
        self.assertEqual((step["workflow"], step["job"], step["step"]), ("Verify PRs", "build", "Test"))
        self.assertEqual(step["weeks"][0], {"week_of": wk.isoformat(), "samples": 5,
                                            "median": "3m", "p90": "10m"})
        self.assertEqual(step["weeks"][1]["samples"], 1)

    def test_only_successful_steps_of_started_jobs_count(self):
        wk = iso(monday(1))
        self.add_job(wk, [("Test", "success", 10 * MIN), ("Test2", "skipped", 0)])
        self.add_job(wk, [("Test", "skipped", 0)])
        self.add_job(wk, [("Test", "failure", 1 * MIN)])
        self.add_job(wk, [("Test", "cancelled", 0)])
        self.add_job(wk, [("Test", "success", 0)], started_at=False)

        out = trends.ci_step_weekly(self.conn, "p")
        self.assertEqual([s["step"] for s in out["steps"]], ["Test"])
        self.assertEqual(out["steps"][0]["weeks"][0]["samples"], 1)
        self.assertEqual(out["steps"][0]["weeks"][0]["median"], "10m")

    def test_steps_are_keyed_by_workflow_and_job_too(self):
        wk = iso(monday(1))
        self.add_job(wk, [("Test", "success", 1 * MIN)], workflow="A", job="j1")
        self.add_job(wk, [("Test", "success", 2 * MIN)], workflow="A", job="j2")
        self.add_job(wk, [("Test", "success", 3 * MIN)], workflow="B", job="j1")
        keys = {(s["workflow"], s["job"]) for s in trends.ci_step_weekly(self.conn, "p")["steps"]}
        self.assertEqual(keys, {("A", "j1"), ("A", "j2"), ("B", "j1")})

    def test_limited_to_top_steps_by_total_time(self):
        wk = iso(monday(1))
        self.add_job(wk, [(f"s{i}", "success", i * MIN) for i in range(1, 6)])
        self.add_job(wk, [("s1", "success", 1 * MIN)] * 1)
        out = trends.ci_step_weekly(self.conn, "p", top=2)
        self.assertEqual([s["step"] for s in out["steps"]], ["s5", "s4"])

    def test_steps_older_than_the_window_are_left_out(self):
        self.add_job(iso(monday(40)), [("Old", "success", MIN)])
        self.add_job(iso(monday(1)), [("New", "success", MIN)])
        out = trends.ci_step_weekly(self.conn, "p")
        self.assertEqual([s["step"] for s in out["steps"]], ["New"])
        self.assertEqual(out["step_history_from"], iso(monday(40)))

    def test_none_without_steps(self):
        self.assertIsNone(trends.ci_step_weekly(self.conn, "p"))

    def test_equal_totals_come_back_in_key_order(self):
        wk = iso(monday(1))
        for name in ("zeta", "alpha", "mid"):
            self.add_job(wk, [(name, "success", MIN)])
        out = trends.ci_step_weekly(self.conn, "p")
        self.assertEqual([s["step"] for s in out["steps"]], ["alpha", "mid", "zeta"])

    def test_only_the_current_week_is_marked_partial(self):
        self.add_job(iso(monday(1)), [("Test", "success", MIN)])
        self.add_job(iso(monday(0), 0), [("Test", "success", MIN)])
        weeks = trends.ci_step_weekly(self.conn, "p")["steps"][0]["weeks"]
        self.assertEqual([(w["week_of"], w.get("partial")) for w in weeks],
                         [(monday(1).isoformat(), None), (monday(0).isoformat(), True)])


class TestLocalTaskSetWeekly(Base):
    def add_build(self, d, tasks, duration, measured_from="build_start", outcome="success", hour=12):
        db.insert_gradle_build(self.conn, {
            "project": "p", "ts": iso(d, hour) if hour < 24 else iso(d), "tasks": tasks,
            "duration_ms": duration, "outcome": outcome, "task_count": 1, "executed": 1,
            "from_cache": 0, "up_to_date": 0, "config_cache": None,
            "measured_from": measured_from, "exec_ms": duration,
            "gradle_version": "9", "daemon_reused": 1,
        })

    def test_build_start_and_execution_are_separate_series(self):
        wk = monday(1)
        for h, d in enumerate((10, 20, 30)):
            self.add_build(wk, "detekt", d * 1000, "build_start", hour=h)
        for h, d in enumerate((2, 3)):
            self.add_build(wk, "detekt", d * 1000, "execution", hour=h)
        out = trends.local_task_set_weekly(self.conn, "p")
        series = {s["measured_from"]: s for s in out["task_sets"]}
        self.assertEqual(series["build_start"]["weeks"][0]["median"], "20s")
        self.assertEqual(series["build_start"]["weeks"][0]["samples"], 3)
        self.assertEqual(series["execution"]["weeks"][0]["samples"], 2)
        self.assertTrue(all(s["tasks"] == "detekt" for s in out["task_sets"]))

    def test_rows_without_measured_from_are_in_neither_series(self):
        self.add_build(monday(1), "detekt", 1000, measured_from=None)
        self.assertIsNone(trends.local_task_set_weekly(self.conn, "p"))

    def test_failed_builds_are_counted_but_not_timed(self):
        wk = monday(1)
        self.add_build(wk, "test", 60_000, hour=1)
        self.add_build(wk, "test", 1_000, outcome="failed", hour=2)
        week = trends.local_task_set_weekly(self.conn, "p")["task_sets"][0]["weeks"][0]
        self.assertEqual((week["runs"], week["failed"], week["samples"]), (2, 1, 1))
        self.assertEqual(week["median"], "60s")

    def test_sleep_outlier_is_excluded_and_reported(self):
        wk = monday(1)
        for h in range(5):
            self.add_build(wk, "detekt", 5_000, hour=h)
        self.add_build(wk, "detekt", 27 * HOUR, hour=6)
        week = trends.local_task_set_weekly(self.conn, "p")["task_sets"][0]["weeks"][0]
        self.assertEqual((week["runs"], week["samples"], week["outliers_excluded"]), (6, 5, 1))
        self.assertEqual(week["p90"], "5s")

    def test_outlier_rule(self):
        def b(i, tasks, d, mf="build_start"):
            return {"id": i, "tasks": tasks, "duration_ms": d, "measured_from": mf,
                    "outcome": "success"}
        builds = [
            b(1, "quick", 5_000), b(2, "quick", 6_000), b(11, "quick", 7_000), b(12, "quick", 8_000),
            b(3, "quick", 40 * MIN),                                               # under floor: kept
            b(4, "quick", 2 * HOUR),                                               # over floor, >10x: out
            b(5, "long", 40 * MIN), b(6, "long", 50 * MIN), b(7, "long", 3 * HOUR),  # <10x: kept
            b(8, "once", 7 * HOUR),                                                # over the cap: out
            b(9, "quick", 2 * HOUR, "execution"),                                  # own series, no ref: kept
            b(10, "quick", None),
        ]
        self.assertEqual(trends.outliers(builds), {4, 8})

    def test_outlier_reference_is_the_median_of_successful_builds(self):
        def b(i, d, outcome="success"):
            return {"id": i, "tasks": "test", "duration_ms": d, "measured_from": "build_start",
                    "outcome": outcome}
        builds = [b(1, 20 * MIN), b(2, 20 * MIN), b(3, 2 * HOUR)]
        builds += [b(10 + i, MIN, "failed") for i in range(5)]
        # With the failed builds the median is 1m and the 2h build is >10x it;
        # successful builds alone put it at 20m, so the 2h build is kept.
        self.assertEqual(trends.outliers(builds), set())

    def test_only_the_current_week_is_marked_partial(self):
        self.add_build(monday(1), "a", 10_000)
        self.add_build(monday(0), "a", 10_000, hour=0)
        weeks = trends.local_task_set_weekly(self.conn, "p")["task_sets"][0]["weeks"]
        self.assertEqual([(w["week_of"], w.get("partial")) for w in weeks],
                         [(monday(1).isoformat(), None), (monday(0).isoformat(), True)])

    def test_most_run_task_sets_first_and_limited(self):
        wk = monday(1)
        for i, (tasks, n) in enumerate((("a", 1), ("b", 3), ("c", 2))):
            for h in range(n):
                self.add_build(wk, tasks, 1000 + i, hour=h)
        out = trends.local_task_set_weekly(self.conn, "p", top=2)
        self.assertEqual([(s["tasks"], s["runs"]) for s in out["task_sets"]], [("b", 3), ("c", 2)])
        self.assertIn("outlier_rule", out)

    def test_weeks_are_separate_buckets(self):
        self.add_build(monday(2), "a", 10_000)
        self.add_build(monday(1), "a", 20_000)
        weeks = trends.local_task_set_weekly(self.conn, "p")["task_sets"][0]["weeks"]
        self.assertEqual([(w["week_of"], w["median"]) for w in weeks],
                         [(monday(2).isoformat(), "10s"), (monday(1).isoformat(), "20s")])


class TestAskContext(CiJobs, Base):
    def test_new_series_reach_the_question_context_and_existing_fields_stay(self):
        self.add_job(iso(monday(1)), [("Test", "success", MIN)])
        db.insert_gradle_build(self.conn, {
            "project": "p", "ts": iso(monday(1)), "tasks": "detekt", "duration_ms": 1000,
            "outcome": "success", "task_count": 1, "executed": 1, "from_cache": 0,
            "up_to_date": 0, "config_cache": None, "measured_from": "build_start",
            "exec_ms": 1000, "gradle_version": "9", "daemon_reused": 1,
        })
        ctx = ask.build_context(self.conn, "p")
        for key in ("ci_weekly_last_26_weeks", "ci_step_weekly_last_26_weeks",
                    "local_task_set_weekly_last_26_weeks", "local_recent_builds"):
            self.assertIn(key, ctx)
        self.assertIn("slow_steps", ctx["ci"])
        self.assertIn("task_sets", ctx["local"])
        json.dumps(ctx)


class TestGradleTaskWeekly(Base):
    def add(self, d, tasks, hour=12, duration=60_000, measured_from="build_start"):
        """One build, with ``tasks`` as [(path, outcome, ms)]."""
        build_id = db.insert_gradle_build(self.conn, {
            "project": "p", "ts": iso(d, hour), "tasks": "build", "duration_ms": duration,
            "outcome": "success", "task_count": len(tasks), "executed": 0, "from_cache": 0,
            "up_to_date": 0, "config_cache": None, "measured_from": measured_from,
            "exec_ms": duration, "gradle_version": "9", "daemon_reused": 1,
        })
        db.insert_gradle_tasks(self.conn, build_id, "p", tasks)

    def test_median_is_over_executed_runs_and_split_counts_everything(self):
        wk = monday(1)
        self.add(wk, [(":app:test", "executed", 10_000)], hour=1)
        self.add(wk, [(":app:test", "executed", 20_000)], hour=2)
        self.add(wk, [(":app:test", "executed", 30_000)], hour=3)
        self.add(wk, [(":app:test", "from_cache", 200)], hour=4)
        self.add(wk, [(":app:test", "up_to_date", 150)], hour=5)
        self.add(wk, [(":app:test", "failed", 1_000)], hour=6)
        week = trends.gradle_task_weekly(self.conn, "p")["tasks"][0]["weeks"][0]
        self.assertEqual(week, {
            "week_of": wk.isoformat(), "runs": 6, "samples": 3,
            "median_executed": "20s", "p90_executed": "30s",
            "executed": 3, "from_cache": 1, "up_to_date": 1, "failed": 1,
        })

    def test_ranked_by_total_time_and_limited(self):
        wk = monday(1)
        self.add(wk, [(":a", "executed", 1_000), (":b", "executed", 5_000), (":c", "executed", 3_000)])
        out = trends.gradle_task_weekly(self.conn, "p", top=2)
        self.assertEqual([(t["task"], t["total_time"]) for t in out["tasks"]], [(":b", "5s"), (":c", "3s")])

    def test_tasks_of_outlier_builds_are_not_timed(self):
        wk = monday(1)
        for h in range(5):
            self.add(wk, [(":lint", "executed", 4_000)], hour=h, duration=5_000)
        self.add(wk, [(":lint", "executed", 20 * HOUR)], hour=6, duration=27 * HOUR)
        week = trends.gradle_task_weekly(self.conn, "p")["tasks"][0]["weeks"][0]
        self.assertEqual((week["runs"], week["samples"], week["p90_executed"]), (6, 5, "4s"))

    def test_one_series_per_task_whatever_the_measurement(self):
        wk = monday(1)
        self.add(wk, [(":a", "executed", 1_000)], hour=1, measured_from="build_start")
        self.add(wk, [(":a", "executed", 3_000)], hour=2, measured_from="execution")
        out = trends.gradle_task_weekly(self.conn, "p")
        self.assertEqual(len(out["tasks"]), 1)
        self.assertEqual(out["tasks"][0]["weeks"][0]["samples"], 2)

    def test_none_before_any_task_detail(self):
        self.assertIsNone(trends.gradle_task_weekly(self.conn, "p"))

    def test_reaches_the_question_context(self):
        self.add(monday(1), [(":a", "executed", 1_000)])
        self.assertIn("local_task_weekly_last_26_weeks", ask.build_context(self.conn, "p"))

    def test_ranking_ties_break_on_path_and_skip_tasks_outside_the_window(self):
        self.add(monday(40), [(":old", "executed", 99_000)], hour=1)
        self.add(monday(1), [(":z", "executed", 2_000), (":y", "executed", 2_000),
                             (":x", "executed", 1_000)], hour=2)
        out = trends.gradle_task_weekly(self.conn, "p", top=2)
        self.assertEqual([t["task"] for t in out["tasks"]], [":y", ":z"])

    def test_only_the_current_week_is_marked_partial(self):
        self.add(monday(1), [(":a", "executed", 1_000)], hour=1)
        self.add(monday(0), [(":a", "executed", 1_000)], hour=0)
        weeks = trends.gradle_task_weekly(self.conn, "p")["tasks"][0]["weeks"]
        self.assertEqual([(w["week_of"], w.get("partial")) for w in weeks],
                         [(monday(1).isoformat(), None), (monday(0).isoformat(), True)])


if __name__ == "__main__":
    unittest.main()

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401

from buildloop import db, insights
from buildloop.humanize import count, ms, pct


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestHumanize(unittest.TestCase):
    def test_durations_use_the_coarsest_informative_unit(self):
        self.assertEqual(ms(8_000), "8s")
        self.assertEqual(ms(89_000), "89s")
        self.assertEqual(ms(90_000), "2m")
        self.assertEqual(ms(1_457_000), "24m")
        self.assertEqual(ms(6 * 3600 * 1000), "6.0h")

    def test_missing_values_render_as_a_dash_not_a_zero(self):
        # A zero would read as "instant"; a dash reads as "unknown".
        self.assertEqual(ms(None), "—")
        self.assertEqual(pct(None), "—")
        self.assertEqual(count(None), "—")


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")
        self.next_id = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_run(self, days, conclusion, exec_ms, workflow="Verify PRs"):
        ts = days_ago(days)
        self.next_id += 1
        db.upsert_run(self.conn, {
            "run_id": self.next_id,
            "project": "p", "workflow": workflow, "branch": "b", "event": "push",
            "status": "completed", "conclusion": conclusion, "attempt": 1,
            "created_at": ts, "started_at": ts, "updated_at": ts,
            "queue_ms": 1000, "exec_ms": exec_ms,
        })

    def add_build(self, days, cc, measured_from, duration_ms):
        db.insert_gradle_build(self.conn, {
            "project": "p", "ts": days_ago(days), "tasks": f":a:b{duration_ms}{cc}",
            "duration_ms": duration_ms, "outcome": "success", "task_count": 10,
            "executed": 2, "from_cache": 3, "up_to_date": 5, "config_cache": cc,
            "measured_from": measured_from, "exec_ms": duration_ms,
            "gradle_version": "9.7.1", "daemon_reused": 1,
        })

    def test_empty_project_yields_no_sections(self):
        s = insights.summarize(self.conn, "p")
        self.assertIsNone(s["ci"])
        self.assertIsNone(s["local"])

    def test_recent_and_previous_windows_are_separated(self):
        for _ in range(4):
            self.add_run(3, "success", 60_000)
        self.add_run(5, "failure", 60_000)
        for _ in range(2):
            self.add_run(40, "success", 120_000)

        wf = insights.summarize(self.conn, "p")["ci"]["workflows"]["Verify PRs"]
        self.assertEqual(wf["recent"]["runs"], 5)
        self.assertEqual(wf["previous"]["runs"], 2)
        self.assertEqual(wf["recent"]["failure_rate_pct"], 20.0)
        self.assertEqual(wf["previous"]["failure_rate_pct"], 0.0)

    def test_durations_reach_the_model_in_human_units(self):
        # A model handed 1457000 writes "1,457,000 ms" into the prose.
        self.add_run(3, "success", 1_457_000)
        wf = insights.summarize(self.conn, "p")["ci"]["workflows"]["Verify PRs"]
        self.assertEqual(wf["recent"]["median_exec"], "24m")

    def test_local_keeps_the_two_duration_populations_apart(self):
        self.add_build(2, "miss", "build_start", 5_000)
        self.add_build(2, "hit", "execution", 40)
        local = insights.summarize(self.conn, "p")["local"]
        self.assertEqual(local["recent"]["median_full_build"], "5s")
        self.assertEqual(local["recent"]["median_cached_config_build"], "0s")
        self.assertEqual(local["recent"]["config_cache_hit_rate_pct"], 50.0)

    def test_builds_with_unknown_config_cache_are_not_counted_as_misses(self):
        self.add_build(2, "hit", "execution", 40)
        self.add_build(2, None, "build_start", 5_000)
        local = insights.summarize(self.conn, "p")["local"]
        self.assertEqual(local["recent"]["config_cache_hit_rate_pct"], 100.0)


class TestFingerprint(unittest.TestCase):
    def test_same_summary_same_fingerprint(self):
        a = {"x": 1, "y": [1, 2]}
        self.assertEqual(insights.fingerprint(a), insights.fingerprint({"y": [1, 2], "x": 1}))

    def test_changed_numbers_change_the_fingerprint(self):
        self.assertNotEqual(insights.fingerprint({"x": 1}), insights.fingerprint({"x": 2}))


class TestPrompt(unittest.TestCase):
    def test_prompt_forbids_comparing_the_two_duration_populations(self):
        prompt = insights.build_prompt({"project": "p"})
        self.assertIn("median_full_build", prompt)
        self.assertIn("median_cached_config_build", prompt)
        self.assertIn("never be presented as a", prompt)

    def test_prompt_forbids_unit_conversion(self):
        self.assertIn("never convert them", insights.build_prompt({}))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import support  # noqa: F401

from buildloop import ci_collector as ci, db

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class TestDurationArithmetic(unittest.TestCase):
    def test_queue_and_execution_are_split(self):
        run = fixture("runs_page.json")["workflow_runs"][0]
        row = ci.map_run(run, "p")
        self.assertEqual(row["queue_ms"], 30_000)      # created -> started
        self.assertEqual(row["exec_ms"], 12 * 60_000)  # started -> updated

    def test_missing_start_falls_back_to_created(self):
        # An in-progress run has no run_started_at; queue time is then 0, not
        # a negative or absurd number.
        run = fixture("runs_page.json")["workflow_runs"][1]
        row = ci.map_run(run, "p")
        self.assertEqual(row["started_at"], row["created_at"])
        self.assertEqual(row["queue_ms"], 0)

    def test_run_attempt_is_preserved(self):
        rows = [ci.map_run(r, "p") for r in fixture("runs_page.json")["workflow_runs"]]
        self.assertEqual([r["attempt"] for r in rows], [1, 2])

    def test_negative_interval_becomes_null_not_a_lie(self):
        self.assertIsNone(ci.delta_ms("2026-01-01T00:00:10Z", "2026-01-01T00:00:00Z"))

    def test_unparseable_timestamps_degrade(self):
        self.assertIsNone(ci.delta_ms("garbage", "2026-01-01T00:00:00Z"))
        self.assertIsNone(ci.delta_ms(None, None))

    def test_bulky_fields_are_discarded(self):
        row = ci.map_run(fixture("runs_page.json")["workflow_runs"][0], "p")
        self.assertNotIn("repository", row)
        self.assertNotIn("pull_requests", row)


class TestJobAndStepMapping(unittest.TestCase):
    def test_job_duration_and_runner(self):
        job = fixture("jobs.json")["jobs"][0]
        row = ci.map_job(job, "p")
        self.assertEqual(row["duration_ms"], 690_000)
        self.assertEqual(row["runner"], "ubuntu-latest")

    def test_steps_ride_along_and_tolerate_missing_timings(self):
        steps = ci.map_steps(fixture("jobs.json")["jobs"][0], "p")
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[1]["duration_ms"], 680_000)
        self.assertIsNone(steps[2]["duration_ms"])


class TestIdempotency(unittest.TestCase):
    """Ingesting a fixture twice must not double-count."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _ingest_once(self):
        for raw in fixture("runs_page.json")["workflow_runs"]:
            db.upsert_run(self.conn, ci.map_run(raw, "p"))
        for raw in fixture("jobs.json")["jobs"]:
            db.upsert_job(self.conn, ci.map_job(raw, "p"))
            for step in ci.map_steps(raw, "p"):
                db.upsert_step(self.conn, step)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_double_ingest_is_a_no_op(self):
        self._ingest_once()
        before = (self.count("ci_run"), self.count("ci_job"), self.count("ci_step"))
        self._ingest_once()
        self.assertEqual(before, (self.count("ci_run"), self.count("ci_job"), self.count("ci_step")))
        self.assertEqual(before, (2, 1, 3))

    def test_upsert_lands_the_terminal_conclusion(self):
        self._ingest_once()
        later = fixture("runs_page.json")["workflow_runs"][1]
        later.update({"status": "completed", "conclusion": "failure",
                      "run_started_at": "2026-09-02T08:00:20Z",
                      "updated_at": "2026-09-02T08:05:00Z"})
        db.upsert_run(self.conn, ci.map_run(later, "p"))
        row = self.conn.execute("SELECT * FROM ci_run WHERE run_id = 102").fetchone()
        self.assertEqual(self.count("ci_run"), 2)
        self.assertEqual(row["conclusion"], "failure")
        self.assertEqual(row["queue_ms"], 20_000)

    def test_jobs_synced_flag_survives_a_run_upsert(self):
        # Re-fetching a run must not silently un-mark its jobs and cause the
        # collector to re-request them forever.
        self._ingest_once()
        db.mark_jobs_synced(self.conn, 101)
        db.upsert_run(self.conn, ci.map_run(fixture("runs_page.json")["workflow_runs"][0], "p"))
        row = self.conn.execute("SELECT jobs_synced FROM ci_run WHERE run_id = 101").fetchone()
        self.assertEqual(row["jobs_synced"], 1)


class TestCutoffFormat(unittest.TestCase):
    def test_cutoff_matches_githubs_timestamp_shape(self):
        # Must not carry microseconds or a +00:00 offset, or boundary rows
        # start depending on lexicographic accidents against "...:00Z".
        cutoff = ci._iso_days_ago(90)
        self.assertRegex(cutoff, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_cutoff_sorts_correctly_against_a_github_timestamp(self):
        recent = ci._iso_days_ago(1)
        old = ci._iso_days_ago(365)
        self.assertLess(old, recent)


class FakeGh:
    """Stands in for `gh api`, recording the windows it was asked for."""

    def __init__(self, totals):
        self.totals = totals  # {(since, until): total_count}
        self.calls = []

    def __call__(self, path, params=None):
        since, until = params["created"].split("..")
        self.calls.append((since, until, params["page"]))
        total = self.totals.get((since, until), 0)
        page = params["page"]
        reachable = min(total, ci.SEARCH_CAP)
        start = (page - 1) * ci.PAGE_SIZE
        runs = [
            {"id": 1_000_000 + start + i, "name": "W", "status": "completed",
             "conclusion": "success", "created_at": f"{since}T00:00:00Z",
             "run_started_at": f"{since}T00:00:00Z", "updated_at": f"{since}T00:01:00Z"}
            for i in range(max(0, min(ci.PAGE_SIZE, reachable - start)))
        ]
        return {"total_count": total, "workflow_runs": runs}


class TestPaginationBoundaries(unittest.TestCase):
    def _fetch(self, fake, since, until):
        original = ci.gh.api
        ci.gh.api = fake
        try:
            return list(ci.RunFetcher("o/r", ci.CollectStats()).iter_runs(since, until))
        finally:
            ci.gh.api = original

    def test_window_under_the_cap_is_paged_not_split(self):
        a, b = date(2026, 1, 1), date(2026, 1, 31)
        fake = FakeGh({(a.isoformat(), b.isoformat()): 250})
        runs = self._fetch(fake, a, b)
        self.assertEqual(len(runs), 250)
        self.assertEqual([c[2] for c in fake.calls], [1, 2, 3])

    def test_exactly_the_cap_is_not_split(self):
        a, b = date(2026, 1, 1), date(2026, 1, 31)
        fake = FakeGh({(a.isoformat(), b.isoformat()): ci.SEARCH_CAP})
        self._fetch(fake, a, b)
        self.assertEqual({(c[0], c[1]) for c in fake.calls}, {(a.isoformat(), b.isoformat())})

    def test_over_the_cap_bisects_the_window(self):
        a, b = date(2026, 1, 1), date(2026, 1, 31)
        mid = a + (b - a) // 2
        fake = FakeGh({
            (a.isoformat(), b.isoformat()): ci.SEARCH_CAP + 1,
            (a.isoformat(), mid.isoformat()): 10,
            ((mid + timedelta(days=1)).isoformat(), b.isoformat()): 20,
        })
        runs = self._fetch(fake, a, b)
        self.assertEqual(len(runs), 30)
        windows = {(c[0], c[1]) for c in fake.calls}
        self.assertIn((a.isoformat(), mid.isoformat()), windows)

    def test_single_day_over_the_cap_terminates(self):
        # Cannot be split further. Must take what it can rather than loop.
        a = date(2026, 1, 1)
        fake = FakeGh({(a.isoformat(), a.isoformat()): ci.SEARCH_CAP + 500})
        runs = self._fetch(fake, a, a)
        self.assertEqual(len(runs), ci.SEARCH_CAP)


if __name__ == "__main__":
    unittest.main()

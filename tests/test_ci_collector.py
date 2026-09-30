import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401

from buildloop import ci_collector as ci, db
from buildloop.config import Project

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


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRepo:
    """Stands in for `gh api` across a whole `collect()`: one repo whose runs
    and jobs the test mutates between refreshes."""

    def __init__(self):
        self.runs = {}   # run_id -> run payload
        self.jobs = {}   # run_id -> job payloads of the latest attempt
        self.job_requests = []

    def __call__(self, path, params=None):
        parts = path.split("/")
        if len(parts) == 3:  # repos/o/r
            return {"created_at": _ago(days=30)}
        if parts[-1] == "runs":
            runs = list(self.runs.values()) if params["page"] == 1 else []
            return {"total_count": len(self.runs), "workflow_runs": runs}
        if parts[-1] == "jobs":
            run_id = int(parts[-2])
            self.job_requests.append(run_id)
            return {"jobs": self.jobs.get(run_id, [])}
        return self.runs[int(parts[-1])]


class TestJobsOfARunCaughtMidFlight(unittest.TestCase):
    """Job detail fetched while a run is still going is a snapshot, not the
    answer. It must be fetched again once the run has finished."""

    RUN = 501

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")
        self.repo = FakeRepo()
        self.project = Project(name="p", github_repo="o/r")
        self._originals = (ci.gh.api, ci.gh.ensure_available)
        ci.gh.api = self.repo
        ci.gh.ensure_available = lambda: None

    def tearDown(self):
        ci.gh.api, ci.gh.ensure_available = self._originals
        self.conn.close()
        self.tmp.cleanup()

    def refresh(self):
        return ci.collect(self.conn, self.project, log=lambda _m: None)

    def run_payload(self, status, conclusion=None, attempt=1, created=None):
        created = created or _ago(minutes=30)
        return {"id": self.RUN, "name": "CI", "status": status, "conclusion": conclusion,
                "run_attempt": attempt, "created_at": created, "run_started_at": created,
                "updated_at": _ago(minutes=1)}

    def job_payload(self, job_id, name, minutes=None, conclusion=None, attempt=1):
        started = _ago(minutes=29)
        completed = _ago(minutes=29 - minutes) if minutes else None
        return {"id": job_id, "run_id": self.RUN, "name": name, "run_attempt": attempt,
                "conclusion": conclusion, "started_at": started, "completed_at": completed,
                "labels": ["ubuntu-latest"],
                "steps": [{"number": 1, "name": "Gradle test", "conclusion": conclusion,
                           "started_at": started, "completed_at": completed}]}

    def jobs(self):
        return {r["job_id"]: r for r in self.conn.execute("SELECT * FROM ci_job")}

    def synced(self):
        return self.conn.execute(
            "SELECT jobs_synced FROM ci_run WHERE run_id = ?", (self.RUN,)).fetchone()[0]

    def test_final_job_data_lands_after_the_run_completes(self):
        self.repo.runs[self.RUN] = self.run_payload("in_progress")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build")]
        self.refresh()
        self.assertIsNone(self.jobs()[1]["duration_ms"])

        self.repo.runs[self.RUN] = self.run_payload("completed", "success")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build", 10, "success")]
        self.refresh()

        job = self.jobs()[1]
        self.assertEqual(job["conclusion"], "success")
        self.assertEqual(job["duration_ms"], 10 * 60_000)
        step = self.conn.execute("SELECT * FROM ci_step WHERE job_id = 1").fetchone()
        self.assertEqual(step["duration_ms"], 10 * 60_000)

    def test_a_job_that_had_not_started_yet_is_picked_up(self):
        self.repo.runs[self.RUN] = self.run_payload("in_progress")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build")]
        self.refresh()

        self.repo.runs[self.RUN] = self.run_payload("completed", "failure")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build", 10, "success"),
                                    self.job_payload(2, "deploy", 3, "cancelled")]
        self.refresh()
        self.assertEqual(self.jobs()[2]["conclusion"], "cancelled")

    def test_a_finished_run_is_asked_for_its_jobs_once(self):
        self.repo.runs[self.RUN] = self.run_payload("completed", "success")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build", 10, "success")]
        self.refresh()
        self.refresh()
        self.refresh()
        self.assertEqual(self.repo.job_requests, [self.RUN])
        self.assertEqual(len(self.jobs()), 1)

    def test_a_rerun_that_came_and_went_between_refreshes_is_fetched(self):
        # Same run id, new attempt, new job ids — and the run is `completed`
        # both times it is seen, so nothing but the attempt says to look again.
        self.repo.runs[self.RUN] = self.run_payload("completed", "failure")
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build", 10, "failure")]
        self.conn.execute(  # already diagnosed, so no log is requested
            "INSERT INTO ci_failure (job_id, project) VALUES (1, 'p')")
        self.refresh()

        self.repo.runs[self.RUN] = self.run_payload("completed", "success", attempt=2)
        self.repo.jobs[self.RUN] = [self.job_payload(7, "build", 8, "success", attempt=2)]
        self.refresh()

        jobs = self.jobs()
        self.assertEqual(jobs[7]["duration_ms"], 8 * 60_000)
        # The first attempt's job is kept: it ran, and it cost minutes.
        self.assertEqual((jobs[1]["conclusion"], jobs[1]["run_attempt"]), ("failure", 1))
        self.assertEqual(self.synced(), 1)

    def test_an_abandoned_run_stops_being_asked_about(self):
        # Never reached a terminal state and is past the point where the run
        # itself is re-checked, so its jobs will not change either.
        created = _ago(days=ci.STALE_RUN_DAYS + 1)
        self.repo.runs[self.RUN] = self.run_payload("in_progress", created=created)
        self.repo.jobs[self.RUN] = [self.job_payload(1, "build")]
        self.refresh()
        self.refresh()
        self.assertEqual(self.repo.job_requests, [self.RUN])

    def test_rows_stranded_by_the_old_collector_are_released(self):
        # What a database written before the fix looks like: the run finished,
        # its jobs are marked as fetched, and one never got its conclusion.
        self.conn.execute("PRAGMA user_version = 2")
        db.upsert_run(self.conn, ci.map_run(self.run_payload("completed", "success"), "p"))
        db.upsert_job(self.conn, ci.map_job(self.job_payload(1, "build"), "p"))
        db.mark_jobs_synced(self.conn, self.RUN)
        db._migrate(self.conn)
        self.assertEqual(self.synced(), 0)
        db.mark_jobs_synced(self.conn, self.RUN)
        db._migrate(self.conn)  # only once: a later connect must not undo it
        self.assertEqual(self.synced(), 1)


if __name__ == "__main__":
    unittest.main()

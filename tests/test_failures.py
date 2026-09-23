import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401

from buildloop import ci_collector as ci, dashboard, db, failures, gh


def log(*entries):
    """Build a GitHub-shaped log from (HH:MM:SS, text) pairs."""
    return "\n".join(f"2026-09-09T{t}.1234567Z {text}" for t, text in entries)


def job(*steps):
    return {"steps": [
        {"number": i, "name": name, "conclusion": conclusion,
         "started_at": f"2026-09-09T{start}Z", "completed_at": f"2026-09-09T{end}Z"}
        for i, (name, conclusion, start, end) in enumerate(steps, 1)
    ]}


ONE_STEP = job(("Build", "failure", "10:00:00", "10:05:00"))


class TestExtraction(unittest.TestCase):
    def reason(self, text, j=ONE_STEP):
        return failures.diagnose(j, text)["reason"]

    def test_an_earlier_steps_error_is_not_the_reason(self):
        # A continue-on-error step can print ##[error] and not fail the job.
        j = job(("Upload", "success", "09:59:00", "09:59:30"),
                ("Check", "failure", "10:00:00", "10:00:10"))
        text = log(("09:59:10", "##[error]Artifact path contains a colon"),
                   ("10:00:05", "##[error]Coverage dropped below the baseline"),
                   ("10:00:05", "##[error]Process completed with exit code 1."))
        d = failures.diagnose(j, text)
        self.assertEqual(d["step"], "Check")
        self.assertEqual(d["reason"], "Coverage dropped below the baseline")

    def test_failing_test_beats_the_gradle_summary(self):
        text = log(("10:01:00", "KoinTest > graph_resolves FAILED"),
                   ("10:01:00", "    java.lang.AssertionError at KoinTest.kt:112"),
                   ("10:01:01", "OtherTest > it_works FAILED"),
                   ("10:01:02", "40 tests completed, 2 failed"),
                   ("10:01:03", "> Task :app:test FAILED"),
                   ("10:01:04", "* What went wrong:"),
                   ("10:01:04", "Execution failed for task ':app:test'."))
        d = failures.diagnose(ONE_STEP, text)
        self.assertEqual(d["reason"], "Test failed: KoinTest > graph_resolves (+1 more)")
        self.assertIn("AssertionError", d["excerpt"])
        self.assertIn("40 tests completed, 2 failed", d["excerpt"])

    def test_kotlin_compile_error_drops_the_runner_path(self):
        text = log(("10:01:00", "e: file:///home/runner/work/App/App/src/Foo.kt:3:38 Unresolved reference 'bar'."),
                   ("10:01:00", "> Task :app:compileKotlin FAILED"))
        self.assertEqual(self.reason(text), "Compile error: Foo.kt:3:38 Unresolved reference 'bar'.")

    def test_gradle_block_takes_the_innermost_cause(self):
        text = log(("10:01:00", "* What went wrong:"),
                   ("10:01:00", "Execution failed for task ':app:compile' (registered by plugin 'x')."),
                   ("10:01:00", "> A failure occurred while executing Work"),
                   ("10:01:00", "- Task `:other` of type `Foo`: interleaved problem"),
                   ("10:01:00", "   > Could not find io.ktor:ktor-client:3.6.0."),
                   ("10:01:00", "     Searched in the following locations:"),
                   ("10:01:00", "* Try:"),
                   ("10:01:00", "> Run with --scan"))
        self.assertEqual(
            self.reason(text),
            "Execution failed for task ':app:compile' — Could not find io.ktor:ktor-client:3.6.0.",
        )

    def test_xcodebuild_failed_commands(self):
        text = log(("10:01:00", "** ARCHIVE FAILED **"),
                   ("10:01:00", "The following build commands failed:"),
                   ("10:01:00", "\tArchiving project iosApp with scheme iosApp"),
                   ("10:01:00", "(1 failure)"),
                   ("10:01:01", "##[error]Process completed with exit code 65."))
        self.assertEqual(self.reason(text), "xcodebuild: Archiving project iosApp with scheme iosApp")

    def test_exit_code_line_alone_is_never_the_reason(self):
        text = log(("10:01:00", "error: could not apply ab7b641... Update report"),
                   ("10:01:01", "##[error]Process completed with exit code 1."))
        self.assertEqual(self.reason(text), "error: could not apply ab7b641... Update report")

    def test_unrecognised_output_falls_back_to_what_preceded_the_exit(self):
        text = log(("10:01:00", "Uploading bundle"),
                   ("10:01:00", "Upload rejected by server"),
                   ("10:01:01", "##[error]Process completed with exit code 2."))
        self.assertEqual(self.reason(text), "Exit code 2 after: Upload rejected by server")

    def test_error_excerpt_shows_the_lead_in_but_not_the_step_header(self):
        text = log(("10:00:00", "Artifact uploaded by the previous step"),
                   ("10:00:01", "##[group]Run ./check-coverage.sh"),
                   ("10:00:01", "env:"),
                   ("10:00:01", "  JAVA_HOME: /opt/java"),
                   ("10:00:01", "##[endgroup]"),
                   ("10:00:02", "Line: 68.05% (9842/14462)"),
                   ("10:00:03", "##[error]Line coverage regressed"))
        excerpt = failures.diagnose(ONE_STEP, text)["excerpt"]
        self.assertIn("68.05%", excerpt)
        self.assertNotIn("JAVA_HOME", excerpt)
        self.assertNotIn("previous step", excerpt)

    def test_ansi_colour_is_stripped(self):
        text = log(("10:01:00", "\x1b[31m##[error]Boom\x1b[0m"))
        self.assertEqual(self.reason(text), "Boom")

    def test_expired_log_keeps_the_step(self):
        d = failures.diagnose(ONE_STEP, None)
        self.assertEqual(d["step"], "Build")
        self.assertIsNone(d["reason"])

    def test_unscopable_step_uses_the_whole_log(self):
        text = log(("08:00:00", "##[error]Runner lost"))
        self.assertEqual(failures.diagnose({}, text)["reason"], "Runner lost")


class TestSignature(unittest.TestCase):
    def test_same_failure_with_different_numbers_groups_together(self):
        a = failures.signature("Line coverage regressed: 9842 covered lines < 9868 (−26)")
        b = failures.signature("Line coverage regressed: 9901 covered lines < 9914 (−13)")
        self.assertEqual(a, b)

    def test_commit_shas_are_ignored(self):
        self.assertEqual(failures.signature("could not apply ab7b641... x"),
                         failures.signature("could not apply cf02794... x"))

    def test_different_failures_stay_apart(self):
        self.assertNotEqual(failures.signature("Detekt found new issues"),
                            failures.signature("Coverage dropped"))


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_job(self, job_id, run_id, name, start, end, conclusion="failure"):
        db.upsert_job(self.conn, {
            "job_id": job_id, "run_id": run_id, "project": "p", "name": name,
            "conclusion": conclusion, "started_at": ago(start), "completed_at": ago(end),
            "duration_ms": 1, "runner": "ubuntu-latest",
        })

    def add_failure(self, job_id, reason, step="Build", excerpt="ctx"):
        db.upsert_failure(self.conn, {
            "job_id": job_id, "project": "p", "step": step, "reason": reason,
            "signature": failures.signature(reason) if reason else None, "excerpt": excerpt,
        })


class TestFailureGroups(DbCase):
    def test_groups_by_signature_and_skips_knock_ons(self):
        # Run 1: build fails, then the required-checks gate fails because of it.
        self.add_job(1, 1, "build", 60, 50)
        self.add_job(2, 1, "required-checks", 49, 48)
        self.add_failure(1, "Coverage regressed: 10 < 12")
        self.add_failure(2, "Required job 'build' concluded with 'failure'", step="Verify")
        # Run 2: the same coverage failure with different numbers.
        self.add_job(3, 2, "build", 30, 20)
        self.add_failure(3, "Coverage regressed: 11 < 14")

        data = dashboard.failure_groups(self.conn, "p")
        self.assertEqual(data["knock_on"], 1)
        self.assertEqual(len(data["groups"]), 1)
        g = data["groups"][0]
        self.assertEqual(g["count"], 2)
        self.assertEqual(g["reason"], "Coverage regressed: 11 < 14")  # the latest wording
        self.assertEqual(g["where"], {"build › Build": 2})

    def test_a_gate_failing_on_a_cancelled_job_is_a_knock_on(self):
        # A newer push cancels the build; the required-checks gate then fails.
        self.add_job(1, 1, "build", 60, 50, conclusion="cancelled")
        self.add_job(2, 1, "required-checks", 49, 48)
        self.add_failure(2, "Required job 'build' concluded with 'cancelled'")
        data = dashboard.failure_groups(self.conn, "p")
        self.assertEqual((data["knock_on"], data["groups"], data["total"]), (1, [], 1))

    def test_parallel_failures_are_both_causes(self):
        # Two jobs that ran side by side each failed on their own.
        self.add_job(1, 1, "android", 60, 40)
        self.add_job(2, 1, "ios", 60, 45)
        self.add_failure(1, "Detekt found issues")
        self.add_failure(2, "Link failed")
        data = dashboard.failure_groups(self.conn, "p")
        self.assertEqual(data["knock_on"], 0)
        self.assertEqual(len(data["groups"]), 2)

    def test_undiagnosed_and_expired_are_counted_not_dropped(self):
        self.add_job(1, 1, "build", 60, 50)
        self.add_job(2, 2, "build", 40, 30)
        self.add_failure(2, None)
        data = dashboard.failure_groups(self.conn, "p")
        self.assertEqual((data["pending"], data["no_log"], data["groups"]), (1, 1, []))

    def test_table_escapes_log_text_and_links_the_job(self):
        self.add_job(1, 7, "build", 60, 50)
        self.add_failure(1, "<script>alert(1)</script>", excerpt="<b>raw</b>")
        html = dashboard.ci_failure_reasons(self.conn, "p", "o/r")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<b>raw</b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("https://github.com/o/r/actions/runs/7/job/1", html)

    def test_rendered_page_includes_the_table(self):
        self.add_job(1, 7, "build", 60, 50)
        self.add_failure(1, "Detekt found new issues")
        page = dashboard.render(self.conn, "p", repo="o/r")
        self.assertIn("Why CI fails", page)
        self.assertIn("Detekt found new issues", page)


class TestCollectFailures(DbCase):
    def test_each_failed_job_is_read_once_and_expiry_is_remembered(self):
        self.add_job(1, 1, "build", 60, 50)
        self.add_job(2, 2, "build", 40, 30)
        self.add_job(3, 3, "build", 20, 10, conclusion="success")
        calls = []

        def fake_api(path, params=None):
            calls.append(path)
            return {"steps": []}

        def fake_text(path):
            calls.append(path)
            if "/jobs/2/" in path:
                raise gh.GhNotFound(path)
            return "2026-09-09T10:00:00.0Z ##[error]Boom"

        project = ci.Project(name="p", github_repo="o/r", gradle_root=None)
        original = gh.api, gh.api_text, gh.rate_limit_remaining
        gh.api, gh.api_text, gh.rate_limit_remaining = fake_api, fake_text, lambda: None
        try:
            stats = ci.CollectStats()
            ci._collect_failures(self.conn, project, stats, lambda _m: None, 90)
            self.assertEqual(stats.failures, 2)
            self.assertEqual(len(calls), 4)  # job + log, for the two failures only
            ci._collect_failures(self.conn, project, ci.CollectStats(), lambda _m: None, 90)
            self.assertEqual(len(calls), 4)  # nothing re-fetched, expired one included
        finally:
            gh.api, gh.api_text, gh.rate_limit_remaining = original

        rows = {r["job_id"]: r["reason"] for r in self.conn.execute("SELECT * FROM ci_failure")}
        self.assertEqual(rows, {1: "Boom", 2: None})

    def run_pass(self, remaining, fail_on=None):
        project = ci.Project(name="p", github_repo="o/r", gradle_root=None)
        calls = []

        def fake_api(path, params=None):
            return {"steps": []}

        def fake_text(path):
            calls.append(path)
            if fail_on and fail_on in path:
                raise gh.GhRateLimited(path)
            return "2026-09-09T10:00:00.0Z ##[error]Boom"

        original = gh.api, gh.api_text, gh.rate_limit_remaining
        gh.api, gh.api_text, gh.rate_limit_remaining = fake_api, fake_text, lambda: remaining
        try:
            stats = ci.CollectStats()
            ci._collect_failures(self.conn, project, stats, lambda _m: None, 90)
        finally:
            gh.api, gh.api_text, gh.rate_limit_remaining = original
        return stats

    def test_a_small_rate_limit_budget_defers_the_rest(self):
        for i in range(1, 6):
            self.add_job(i, i, "build", 100 - i, 90 - i)
        # Two requests per failure, above the reserve: room for two.
        stats = self.run_pass(ci.RATE_LIMIT_RESERVE + 5)
        self.assertEqual(stats.failures, 2)
        stats = self.run_pass(10_000)
        self.assertEqual(stats.failures, 3)

    def test_hitting_the_rate_limit_keeps_progress_and_does_not_raise(self):
        self.add_job(1, 1, "build", 60, 50)
        self.add_job(2, 2, "build", 40, 30)
        stats = self.run_pass(None, fail_on="/jobs/1/")  # job 2 is newer, so first
        self.assertLessEqual(stats.failures, 1)
        stats = self.run_pass(None)
        done = self.conn.execute("SELECT COUNT(*) FROM ci_failure").fetchone()[0]
        self.assertEqual(done, 2)


if __name__ == "__main__":
    unittest.main()

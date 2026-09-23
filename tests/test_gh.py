import subprocess
import unittest
from unittest import mock

import support  # noqa: F401

from buildloop import gh


class TestGetClassification(unittest.TestCase):
    """How a failed `gh api` call is classified decides skip-for-good vs retry-later."""

    def call(self, stderr):
        sleeps = []
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)
        with mock.patch.object(gh.subprocess, "run", return_value=failed) as run, \
                mock.patch.object(gh.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(gh.GhError) as ctx:
                gh.api_text("repos/o/r/actions/jobs/1/logs")
        return ctx.exception, run.call_count, sleeps

    def test_gone_is_not_found(self):
        exc, calls, sleeps = self.call("gh: Gone (HTTP 410)")
        self.assertIsInstance(exc, gh.GhNotFound)
        self.assertEqual((calls, sleeps), (1, []))

    def test_primary_rate_limit_does_not_retry(self):
        exc, calls, sleeps = self.call("gh: API rate limit exceeded for user ID 123456. (HTTP 403)")
        self.assertIsInstance(exc, gh.GhRateLimited)
        self.assertEqual((calls, sleeps), (1, []))

    def test_secondary_rate_limit_does_not_retry(self):
        exc, calls, sleeps = self.call("gh: You have exceeded a secondary rate limit. (HTTP 403)")
        self.assertIsInstance(exc, gh.GhRateLimited)
        self.assertEqual((calls, sleeps), (1, []))

    def test_a_server_error_retries_with_backoff_then_raises_plain_gh_error(self):
        exc, calls, sleeps = self.call("gh: Bad Gateway (HTTP 502)")
        self.assertIs(type(exc), gh.GhError)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [1, 2])


if __name__ == "__main__":
    unittest.main()

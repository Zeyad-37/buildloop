import subprocess
import sys
import unittest
from pathlib import Path

import support  # noqa: F401

ENTRY = Path(__file__).resolve().parent.parent / "bin" / "buildloop"
SYSTEM_PYTHON = Path("/usr/bin/python3")


class TestVersionGuard(unittest.TestCase):
    """macOS ships Python 3.9, which has no tomllib."""

    def test_the_guard_runs_before_any_buildloop_import(self):
        # If an import of ours moved above it, an old interpreter would fail
        # on that import instead and the message would never be seen.
        source = ENTRY.read_text()
        self.assertLess(source.index("sys.version_info < (3, 11)"),
                        source.index("from buildloop.cli import main"))

    @unittest.skipUnless(
        SYSTEM_PYTHON.exists()
        and subprocess.run([SYSTEM_PYTHON, "-c", "import sys; raise SystemExit(sys.version_info >= (3, 11))"]
                           ).returncode == 0,
        "no pre-3.11 interpreter available to test against",
    )
    def test_an_old_interpreter_gets_a_readable_message(self):
        proc = subprocess.run([SYSTEM_PYTHON, ENTRY, "--version"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("needs Python 3.11 or newer", proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_a_supported_interpreter_still_runs(self):
        proc = subprocess.run([sys.executable, ENTRY, "--version"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("buildloop", proc.stdout)


if __name__ == "__main__":
    unittest.main()

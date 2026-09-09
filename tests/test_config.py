import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from buildloop import config


class TestParse(unittest.TestCase):
    def parse(self, raw):
        return config.parse(raw, Path("/tmp/h"))

    def test_reads_projects(self):
        cfg = self.parse({"project": [{"name": "a", "github_repo": "o/r", "gradle_root": "A"}]})
        self.assertEqual(cfg.projects[0].name, "a")
        self.assertTrue(cfg.projects[0].tracks_ci)
        self.assertTrue(cfg.projects[0].tracks_gradle)

    def test_rejects_empty(self):
        with self.assertRaises(config.ConfigError):
            self.parse({})

    def test_rejects_duplicate_name(self):
        with self.assertRaises(config.ConfigError):
            self.parse({"project": [{"name": "a", "gradle_root": "A"}, {"name": "a", "gradle_root": "B"}]})

    def test_rejects_project_with_no_source(self):
        with self.assertRaises(config.ConfigError):
            self.parse({"project": [{"name": "a"}]})

    def test_rejects_bad_repo_slug(self):
        with self.assertRaises(config.ConfigError):
            self.parse({"project": [{"name": "a", "github_repo": "not-a-slug"}]})

    def test_rejects_two_projects_claiming_one_gradle_root(self):
        # Two projects sharing a gradle_root would make every local build
        # ambiguous, so it has to fail loudly at config time.
        with self.assertRaises(config.ConfigError):
            self.parse({"project": [{"name": "a", "gradle_root": "X"}, {"name": "b", "gradle_root": "X"}]})


class TestGradleProjectsFile(unittest.TestCase):
    def test_only_gradle_tracked_projects_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = config.parse(
                {
                    "project": [
                        {"name": "a", "gradle_root": "RootA"},
                        {"name": "ci-only", "github_repo": "o/r"},
                    ]
                },
                home,
            )
            path = config.write_gradle_projects(cfg)
            self.assertEqual(path.read_text(), "RootA\ta\n")

    def test_rewrite_replaces_previous_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config.write_gradle_projects(config.parse({"project": [{"name": "a", "gradle_root": "A"}]}, home))
            config.write_gradle_projects(config.parse({"project": [{"name": "b", "gradle_root": "B"}]}, home))
            self.assertEqual((home / config.GRADLE_PROJECTS_FILENAME).read_text(), "B\tb\n")


if __name__ == "__main__":
    unittest.main()

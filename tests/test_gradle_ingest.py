import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from buildloop import db, gradle_ingest as gi

ROW = {
    "ts": "2026-09-09T16:22:15Z",
    "project": "steady",
    "tasks": [":androidApp:assembleDebug"],
    "duration_ms": 84213,
    "outcome": "success",
    "task_count": 412,
    "executed": 37,
    "from_cache": 118,
    "up_to_date": 257,
    "config_cache": "hit",
    "measured_from": "execution",
    "exec_ms": 80000,
    "gradle_version": "9.7.1",
    "daemon_reused": True,
}


class TestSplitCompleteLines(unittest.TestCase):
    def test_whole_lines_are_all_consumed(self):
        lines, consumed = gi.split_complete_lines(b'{"a":1}\n{"b":2}\n')
        self.assertEqual(len(lines), 2)
        self.assertEqual(consumed, 16)

    def test_trailing_fragment_is_neither_returned_nor_consumed(self):
        # A build killed with Ctrl-C mid-write. The partial line must stay
        # unconsumed so the next ingest sees it whole.
        data = b'{"a":1}\n{"b":'
        lines, consumed = gi.split_complete_lines(data)
        self.assertEqual(len(lines), 1)
        self.assertEqual(consumed, 8)
        self.assertEqual(data[consumed:], b'{"b":')

    def test_empty_buffer(self):
        self.assertEqual(gi.split_complete_lines(b""), ([], 0))

    def test_blank_lines_are_skipped_but_still_consumed(self):
        lines, consumed = gi.split_complete_lines(b'\n\n{"a":1}\n')
        self.assertEqual(len(lines), 1)
        self.assertEqual(consumed, 10)


class TestMapRow(unittest.TestCase):
    def test_maps_a_full_row(self):
        row = gi.map_row(ROW)
        self.assertEqual(row["tasks"], ":androidApp:assembleDebug")
        self.assertEqual(row["config_cache"], "hit")
        self.assertEqual(row["daemon_reused"], 1)

    def test_empty_task_list_is_named_sync(self):
        # An IDE sync is a configuration-only build, not a mystery.
        row = gi.map_row({**ROW, "tasks": []})
        self.assertEqual(row["tasks"], "(sync)")

    def test_unknown_config_cache_value_degrades_to_null(self):
        self.assertIsNone(gi.map_row({**ROW, "config_cache": "maybe"})["config_cache"])
        self.assertIsNone(gi.map_row({**ROW, "config_cache": None})["config_cache"])

    def test_unknown_measured_from_degrades_to_null(self):
        self.assertIsNone(gi.map_row({**ROW, "measured_from": "vibes"})["measured_from"])

    def test_null_metrics_survive(self):
        row = gi.map_row({**ROW, "duration_ms": None, "task_count": None})
        self.assertIsNone(row["duration_ms"])
        self.assertIsNone(row["task_count"])

    def test_rows_without_identity_are_rejected(self):
        self.assertIsNone(gi.map_row({"ts": "x"}))
        self.assertIsNone(gi.map_row({"project": "p"}))
        self.assertIsNone(gi.map_row([]))


class IngestFixture:
    """A temp home, builds.jsonl and database; holds no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.jsonl = self.home / "builds.jsonl"
        self.conn = db.connect(self.home / "t.sqlite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def append(self, *objs, raw=""):
        with self.jsonl.open("a") as fh:
            for o in objs:
                fh.write(json.dumps(o) + "\n")
            fh.write(raw)

    def run_ingest(self):
        return gi.ingest(self.conn, self.jsonl, {"steady"})

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM gradle_build").fetchone()[0]


class TestIngest(IngestFixture, unittest.TestCase):
    def test_missing_file_is_not_an_error(self):
        self.assertEqual(self.run_ingest().inserted, 0)

    def test_basic_ingest(self):
        self.append(ROW, {**ROW, "ts": "2026-09-09T16:30:00Z"})
        self.assertEqual(self.run_ingest().inserted, 2)
        self.assertEqual(self.count(), 2)

    def test_watermark_means_a_second_ingest_reads_nothing(self):
        self.append(ROW)
        self.run_ingest()
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.consumed_bytes, 0)

    def test_only_new_lines_are_ingested(self):
        self.append(ROW)
        self.run_ingest()
        self.append({**ROW, "ts": "2026-09-09T17:00:00Z"})
        self.assertEqual(self.run_ingest().inserted, 1)
        self.assertEqual(self.count(), 2)

    def test_truncated_final_line_is_picked_up_once_completed(self):
        self.append(ROW, raw='{"ts":"2026-09-09T18:00:00Z","project":"stea')
        self.assertEqual(self.run_ingest().inserted, 1)
        # The writer finishes the row on the next build.
        with self.jsonl.open("a") as fh:
            fh.write('dy","tasks":["x"],"duration_ms":1}\n')
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.malformed, 0)

    def test_abandoned_partial_line_is_reported_not_silently_dropped(self):
        self.append(ROW, raw='{"ts":"broken\n')
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.malformed, 1)

    def test_replayed_file_does_not_double_count(self):
        # Offset reset (file replaced/rotated) must fall through to the UNIQUE
        # constraint rather than duplicating history.
        self.append(ROW)
        self.run_ingest()
        db.set_state(self.conn, gi.OFFSET_KEY, "0")
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(self.count(), 1)

    def test_truncated_file_resets_the_offset(self):
        self.append(ROW, {**ROW, "ts": "2026-09-09T16:31:00Z"})
        self.run_ingest()
        self.jsonl.write_text(json.dumps({**ROW, "ts": "2026-09-09T19:00:00Z"}) + "\n")
        self.assertEqual(self.run_ingest().inserted, 1)

    def test_unconfigured_projects_are_skipped(self):
        self.append({**ROW, "project": "someone-elses-app"})
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.unknown_project, 1)

    def test_garbage_lines_do_not_stop_the_ingest(self):
        self.append(ROW)
        with self.jsonl.open("a") as fh:
            fh.write("not json at all\n")
        self.append({**ROW, "ts": "2026-09-09T20:00:00Z"})
        stats = self.run_ingest()
        self.assertEqual(stats.inserted, 2)
        self.assertEqual(stats.malformed, 1)


class TestTaskDetail(IngestFixture, unittest.TestCase):
    """Per-task timings carried on the build line (schema v4)."""

    def recent(self, days=1):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def tasks(self):
        return self.conn.execute(
            "SELECT path, outcome, duration_ms FROM gradle_task ORDER BY path").fetchall()

    def test_task_detail_is_stored_against_its_build(self):
        self.append({**ROW, "ts": self.recent(), "task_detail": [
            [":app:compileKotlin", "executed", 4200],
            [":app:test", "from_cache", 150],
        ]})
        self.assertEqual(self.run_ingest().tasks, 2)
        self.assertEqual([tuple(r) for r in self.tasks()],
                         [(":app:compileKotlin", "executed", 4200), (":app:test", "from_cache", 150)])
        build_id = self.conn.execute("SELECT id FROM gradle_build").fetchone()[0]
        self.assertEqual({r[0] for r in self.conn.execute("SELECT build_id FROM gradle_task")}, {build_id})

    def test_rows_without_detail_still_ingest(self):
        self.append({**ROW, "ts": self.recent()})
        stats = self.run_ingest()
        self.assertEqual((stats.inserted, stats.tasks), (1, 0))

    def test_malformed_entries_are_dropped_not_fatal(self):
        self.assertEqual(gi.map_tasks({"task_detail": [
            [":ok", "up_to_date", 120],
            [":bad-outcome", "weird", 1], [":neg", "executed", -1], ["", "executed", 1],
            [":short", "executed"], "nope", [1, "executed", 5],
        ]}), [(":ok", "up_to_date", 120)])
        self.assertEqual(gi.map_tasks({"task_detail": "x"}), [])

    def test_replayed_line_does_not_duplicate_tasks(self):
        line = {**ROW, "ts": self.recent(), "task_detail": [[":a", "executed", 500]]}
        self.append(line)
        self.run_ingest()
        db.set_state(self.conn, gi.OFFSET_KEY, "0")
        self.run_ingest()
        self.assertEqual(len(self.tasks()), 1)

    def test_old_task_rows_are_pruned_but_builds_kept(self):
        self.append({**ROW, "ts": self.recent(db.GRADLE_TASK_DAYS + 5), "task_detail": [[":old", "executed", 500]]},
                    {**ROW, "ts": self.recent(1), "task_detail": [[":new", "executed", 500]]})
        self.run_ingest()
        self.assertEqual([r["path"] for r in self.tasks()], [":new"])
        self.assertEqual(self.count(), 2)


class TestSchemaV4Migration(unittest.TestCase):
    def test_a_v3_database_gains_the_task_table(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite"
            db.connect(path).close()  # a full schema, then wound back to v3
            raw = sqlite3.connect(path)
            raw.executescript("DROP TABLE gradle_task; PRAGMA user_version = 3;")
            raw.close()
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            conn.execute("SELECT build_id, path, outcome, duration_ms FROM gradle_task").fetchall()
            conn.close()


if __name__ == "__main__":
    unittest.main()

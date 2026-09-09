"""Ingest ``builds.jsonl`` (written by the Gradle init script) into SQLite.

Two things this has to survive, because both happen in normal use:

* **A truncated final line.** Ctrl-C during a build can leave a half-written
  row. The partial line is left in place and not consumed, so the *next*
  ingest picks it up once the writer finishes it — or skips it as malformed if
  the build died. Either way the byte offset never advances past bytes we did
  not successfully parse.
* **Re-ingest.** The byte offset is the fast path; the UNIQUE constraint on
  ``gradle_build`` is the correctness backstop. Ingesting the same file twice
  cannot double-count.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

OFFSET_KEY = "gradle_jsonl_offset"

_FIELDS = (
    "project", "ts", "tasks", "duration_ms", "outcome", "task_count",
    "executed", "from_cache", "up_to_date", "config_cache",
    "gradle_version", "daemon_reused",
)


@dataclass
class IngestStats:
    inserted: int = 0
    duplicates: int = 0
    malformed: int = 0
    unknown_project: int = 0
    consumed_bytes: int = 0

    def __str__(self) -> str:
        parts = [f"{self.inserted} build(s)"]
        if self.duplicates:
            parts.append(f"{self.duplicates} already stored")
        if self.malformed:
            parts.append(f"{self.malformed} malformed")
        if self.unknown_project:
            parts.append(f"{self.unknown_project} from unconfigured projects")
        return ", ".join(parts)


def map_row(raw: dict) -> dict | None:
    """Normalise one JSONL object into a ``gradle_build`` row.

    Returns None when the row lacks the fields that make it meaningful.
    """
    if not isinstance(raw, dict):
        return None
    project, ts = raw.get("project"), raw.get("ts")
    if not isinstance(project, str) or not isinstance(ts, str) or not project or not ts:
        return None

    tasks = raw.get("tasks")
    if isinstance(tasks, list):
        tasks_text = " ".join(str(t) for t in tasks)
    else:
        tasks_text = "" if tasks is None else str(tasks)

    row = {f: raw.get(f) for f in _FIELDS}
    row["project"] = project
    row["ts"] = ts
    # An empty task list is Android Studio sync (a configuration-only build),
    # not a mystery. Naming it keeps it distinguishable in the charts.
    row["tasks"] = tasks_text or "(sync)"
    row["duration_ms"] = _as_int(raw.get("duration_ms"))
    for key in ("task_count", "executed", "from_cache", "up_to_date", "exec_ms"):
        row[key] = _as_int(raw.get(key))
    cc = raw.get("config_cache")
    row["config_cache"] = cc if cc in ("hit", "miss") else None
    mf = raw.get("measured_from")
    row["measured_from"] = mf if mf in ("build_start", "execution") else None
    daemon = raw.get("daemon_reused")
    row["daemon_reused"] = None if daemon is None else int(bool(daemon))
    outcome = raw.get("outcome")
    row["outcome"] = outcome if isinstance(outcome, str) else None
    gv = raw.get("gradle_version")
    row["gradle_version"] = gv if isinstance(gv, str) else None
    return row


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def split_complete_lines(data: bytes) -> tuple[list[bytes], int]:
    """Split a buffer into whole lines, returning them and the bytes consumed.

    A trailing fragment with no newline is *not* returned and *not* counted as
    consumed — that is the truncation tolerance.
    """
    if not data:
        return [], 0
    parts = data.split(b"\n")
    trailing = parts[-1]
    complete = [p for p in parts[:-1] if p.strip()]
    return complete, len(data) - len(trailing)


def ingest(conn: sqlite3.Connection, jsonl_path: Path, known_projects: set[str],
           *, offset_key: str = OFFSET_KEY) -> IngestStats:
    from . import db

    stats = IngestStats()
    if not jsonl_path.exists():
        return stats

    offset = int(db.get_state(conn, offset_key) or 0)
    size = jsonl_path.stat().st_size
    if offset > size:
        # The file was truncated or replaced. Re-read from the start; the
        # UNIQUE constraint keeps already-stored builds from duplicating.
        offset = 0

    with jsonl_path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()

    lines, consumed = split_complete_lines(data)
    for line in lines:
        try:
            raw = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats.malformed += 1
            continue
        row = map_row(raw)
        if row is None:
            stats.malformed += 1
            continue
        if row["project"] not in known_projects:
            stats.unknown_project += 1
            continue
        if db.insert_gradle_build(conn, row):
            stats.inserted += 1
        else:
            stats.duplicates += 1

    stats.consumed_bytes = consumed
    db.set_state(conn, offset_key, str(offset + consumed))
    conn.commit()
    return stats

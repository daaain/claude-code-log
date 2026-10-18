#!/usr/bin/env python3
"""Archive detection resolves sessions through real source files, not stems.

A cached session is "archived" only when its source JSONL has actually been
deleted. Because a JSONL can hold several session IDs, and its filename stem
need not equal any of them (e.g. after a rename), matching session IDs against
filename stems alone wrongly flags live sessions as archived. These tests pin
`CacheManager.get_existing_session_ids` and the archive lookups that use it.
"""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from claude_code_log.cache import CacheManager


class ArchiveDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(
            """
            CREATE TABLE cached_files(id INTEGER PRIMARY KEY, project_id INTEGER, file_path TEXT);
            CREATE TABLE messages(project_id INTEGER, file_id INTEGER, session_id TEXT);
            CREATE TABLE sessions(project_id INTEGER, session_id TEXT, hidden INTEGER DEFAULT 0,
              summary TEXT, ai_title TEXT, first_timestamp TEXT DEFAULT '', last_timestamp TEXT DEFAULT '',
              message_count INTEGER DEFAULT 0, first_user_message TEXT DEFAULT '', cwd TEXT,
              total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
              total_cache_creation_tokens INTEGER DEFAULT 0, total_cache_read_tokens INTEGER DEFAULT 0,
              team_name TEXT);
            """
        )
        # Build a CacheManager without touching its real on-disk schema/connection:
        # exercise the archive logic in isolation against the stub connection.
        self.manager = object.__new__(CacheManager)
        self.manager._project_id = 1
        self.manager._get_connection = lambda: contextlib.nullcontext(self.db)

    def source(self, sid, name="different.jsonl", project=1, present=True, hidden=0):
        """Record a cached session `sid` sourced from file `name` in `project`."""
        path = self.root / name
        if present:
            path.write_text("{}\n")
        fid = self.db.execute(
            "INSERT INTO cached_files(project_id,file_path) VALUES (?,?)",
            (project, str(path)),
        ).lastrowid
        self.db.execute("INSERT INTO messages VALUES (?,?,?)", (project, fid, sid))
        self.db.execute(
            "INSERT INTO sessions(project_id,session_id,hidden) VALUES (?,?,?)",
            (project, sid, hidden),
        )
        return path

    def test_renamed_source_is_not_archived(self):
        # Session id differs from the filename stem; the file still exists.
        p = self.source("embedded-id")
        self.assertEqual(self.manager.get_archived_session_count({p.stem}), 0)
        self.assertEqual(self.manager.get_archived_sessions({p.stem}), {})
        self.assertIn("embedded-id", self.manager.get_existing_session_ids({p.stem}))

    def test_multiple_sessions_in_one_file(self):
        self.source("first")
        self.source("second")
        self.assertEqual(
            self.manager.get_existing_session_ids(set()), {"first", "second"}
        )
        self.assertEqual(self.manager.get_archived_session_count(set()), 0)

    def test_deleted_source_stays_archived(self):
        self.source("gone", present=False)
        self.assertEqual(self.manager.get_archived_session_count(set()), 1)
        self.assertEqual(set(self.manager.get_archived_sessions(set())), {"gone"})

    def test_one_surviving_copy_suffices(self):
        # Same session recorded from two files; one deleted, one still present.
        self.source("kept", present=False)
        p = self.root / "survivor.jsonl"
        p.write_text("{}\n")
        fid = self.db.execute(
            "INSERT INTO cached_files(project_id,file_path) VALUES (1,?)", (str(p),)
        ).lastrowid
        self.db.execute("INSERT INTO messages VALUES (1,?,?)", (fid, "kept"))
        self.assertEqual(self.manager.get_archived_session_count(set()), 0)

    def test_other_project_cannot_rescue_deleted_session(self):
        # A surviving file in another project must not clear this project's orphan.
        self.source("same-id", "gone.jsonl", present=False)
        self.source("same-id", "present.jsonl", project=2)
        self.assertEqual(self.manager.get_archived_session_count(set()), 1)

    def test_hidden_sessions_remain_excluded(self):
        self.source("hidden", present=False, hidden=1)
        self.assertEqual(self.manager.get_archived_session_count(set()), 0)
        self.assertEqual(self.manager.get_archived_sessions(set()), {})

    def test_uncached_filename_fallback_and_no_input_mutation(self):
        # Stems for uncached files survive; the caller's set is not mutated.
        names = {"uncached"}
        self.source("cached")
        self.assertEqual(
            self.manager.get_existing_session_ids(names), {"cached", "uncached"}
        )
        self.assertEqual(names, {"uncached"})

    def test_directory_is_not_a_source_file(self):
        # A directory sharing the recorded path must not count as a present source.
        p = self.source("not-file", present=False)
        p.mkdir()
        self.assertEqual(self.manager.get_archived_session_count(set()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

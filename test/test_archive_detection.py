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
            CREATE TABLE messages(project_id INTEGER, file_id INTEGER, session_id TEXT,
              _is_sidechain INTEGER DEFAULT 0);
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

    def source(
        self,
        sid,
        name="different.jsonl",
        project=1,
        present=True,
        hidden=0,
        sidechain=0,
        add_session=True,
    ):
        """Record a cached session `sid` sourced from file `name` in `project`.

        `sidechain=1` models an ``agent-*`` sub-transcript row (the trunk
        session ID recorded against an agent file), which must not keep a
        session current once its trunk JSONL is gone. Pass `add_session=False`
        when adding a second source file for a session that already has its row
        (the real `sessions` table is unique per `(project, session_id)`).
        """
        path = self.root / name
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        fid = self.db.execute(
            "INSERT INTO cached_files(project_id,file_path) VALUES (?,?)",
            (project, str(path)),
        ).lastrowid
        self.db.execute(
            "INSERT INTO messages(project_id,file_id,session_id,_is_sidechain) "
            "VALUES (?,?,?,?)",
            (project, fid, sid, sidechain),
        )
        if add_session:
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
        self.db.execute(
            "INSERT INTO messages(project_id,file_id,session_id) VALUES (1,?,?)",
            (fid, "kept"),
        )
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

    def test_surviving_agent_file_does_not_rescue_deleted_trunk(self):
        # Trunk JSONL deleted; a sub-agent file carrying the trunk session id
        # survives. The session is archived, not current: trunk discovery
        # cannot load it from an agent sub-transcript.
        self.source("trunk", "trunk.jsonl", present=False)
        self.source("trunk", "subagents/agent-x.jsonl", present=True, sidechain=1, add_session=False)
        self.assertNotIn("trunk", self.manager.get_existing_session_ids(set()))
        self.assertEqual(self.manager.get_archived_session_count(set()), 1)
        self.assertEqual(set(self.manager.get_archived_sessions(set())), {"trunk"})

    def test_surviving_trunk_with_agent_file_is_current(self):
        # Both trunk and its agent file present: the session stays current.
        self.source("trunk", "trunk.jsonl", present=True)
        self.source("trunk", "subagents/agent-x.jsonl", present=True, sidechain=1, add_session=False)
        self.assertIn("trunk", self.manager.get_existing_session_ids(set()))
        self.assertEqual(self.manager.get_archived_session_count(set()), 0)

    def test_source_paths_resolve_real_trunk_only(self):
        # get_session_source_paths returns the renamed trunk file, never the
        # agent sub-transcript, and only files that still exist.
        trunk = self.source("renamed", "embedded.jsonl", present=True)
        self.source("renamed", "subagents/agent-x.jsonl", present=True, sidechain=1, add_session=False)
        self.assertEqual(
            self.manager.get_session_source_paths("renamed"), [str(trunk)]
        )
        # Including sidechain rows surfaces the agent file too.
        self.assertEqual(
            len(self.manager.get_session_source_paths("renamed", trunk_only=False)), 2
        )

    def test_source_paths_omit_deleted_files(self):
        self.source("gone", "gone.jsonl", present=False)
        self.assertEqual(self.manager.get_session_source_paths("gone"), [])

    def test_sessions_sharing_file_reports_cohabitants(self):
        # Two sessions written into one JSONL; each sees the other as sharing.
        shared = self.root / "shared.jsonl"
        shared.write_text("{}\n")
        fid = self.db.execute(
            "INSERT INTO cached_files(project_id,file_path) VALUES (1,?)",
            (str(shared),),
        ).lastrowid
        for sid in ("alpha", "beta"):
            self.db.execute(
                "INSERT INTO messages(project_id,file_id,session_id) VALUES (1,?,?)",
                (fid, sid),
            )
            self.db.execute(
                "INSERT INTO sessions(project_id,session_id,hidden) VALUES (1,?,0)",
                (sid,),
            )
        self.assertEqual(
            self.manager.get_sessions_sharing_file(str(shared), "alpha"), {"beta"}
        )
        # A file holding a single session reports no cohabitants.
        solo = self.source("solo", "solo.jsonl")
        self.assertEqual(
            self.manager.get_sessions_sharing_file(str(solo), "solo"), set()
        )

    def test_sessions_sharing_file_excludes_hidden_and_agent_rows(self):
        shared = self.root / "shared.jsonl"
        shared.write_text("{}\n")
        fid = self.db.execute(
            "INSERT INTO cached_files(project_id,file_path) VALUES (1,?)",
            (str(shared),),
        ).lastrowid
        # Visible cohabitant (trunk row).
        self.db.execute(
            "INSERT INTO messages(project_id,file_id,session_id) VALUES (1,?,?)",
            (fid, "visible"),
        )
        self.db.execute(
            "INSERT INTO sessions(project_id,session_id,hidden) VALUES (1,?,0)",
            ("visible",),
        )
        # Hidden cohabitant and an agent (sidechain) row must both be ignored.
        self.db.execute(
            "INSERT INTO messages(project_id,file_id,session_id) VALUES (1,?,?)",
            (fid, "hidden-one"),
        )
        self.db.execute(
            "INSERT INTO sessions(project_id,session_id,hidden) VALUES (1,?,1)",
            ("hidden-one",),
        )
        self.db.execute(
            "INSERT INTO messages(project_id,file_id,session_id,_is_sidechain) "
            "VALUES (1,?,?,1)",
            (fid, "agent-sid"),
        )
        self.assertEqual(
            self.manager.get_sessions_sharing_file(str(shared), "self"), {"visible"}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

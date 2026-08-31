#!/usr/bin/env python3
"""Comprehensive SQL-level integrity tests for SQLite cache."""

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from claude_code_log.cache import (
    CacheManager,
    SessionCacheData,
    _migrated_db_paths,
    discard_database_files,
    is_corrupt_database_error,
)
from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    UsageInfo,
    UserMessageModel,
    UserTranscriptEntry,
)

from test.conftest import bump_mtime


# Use conftest.py fixtures: isolated_cache_dir, isolated_db_path, isolated_cache_manager


@pytest.fixture
def cache_manager(isolated_cache_dir: Path, isolated_db_path: Path) -> CacheManager:
    """Create a cache manager with explicit db_path for test isolation."""
    return CacheManager(isolated_cache_dir, "1.0.0", db_path=isolated_db_path)


@pytest.fixture
def sample_user_entry():
    """Create a sample user transcript entry."""
    return UserTranscriptEntry(
        type="user",
        uuid="user-123",
        timestamp="2024-01-01T10:00:00Z",
        sessionId="session-1",
        version="1.0.0",
        parentUuid=None,
        isSidechain=False,
        userType="external",
        cwd="/test/path",
        message=UserMessageModel(
            role="user", content=[TextContent(type="text", text="Hello, world!")]
        ),
    )


@pytest.fixture
def sample_assistant_entry():
    """Create a sample assistant transcript entry with token usage."""
    return AssistantTranscriptEntry(
        type="assistant",
        uuid="assistant-123",
        timestamp="2024-01-01T10:01:00Z",
        sessionId="session-1",
        version="1.0.0",
        parentUuid="user-123",
        isSidechain=False,
        userType="assistant",
        cwd="/test/path",
        requestId="req-123",
        message=AssistantMessageModel(
            id="msg-123",
            type="message",
            role="assistant",
            model="claude-3",
            content=[TextContent(type="text", text="Hi there!")],
            usage=UsageInfo(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
            ),
        ),
    )


class TestCascadeDelete:
    """Tests for cascade delete behaviour."""

    def test_cascade_delete_project_removes_all_nested_records(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
    ):
        """Deleting project cascades to files, messages, sessions."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create a JSONL file with entries
        jsonl_file = isolated_cache_dir / "test.jsonl"
        jsonl_file.write_text(
            json.dumps(sample_user_entry.model_dump())
            + "\n"
            + json.dumps(sample_assistant_entry.model_dump())
            + "\n",
            encoding="utf-8",
        )

        # Save entries to cache
        cache_manager.save_cached_entries(
            jsonl_file, [sample_user_entry, sample_assistant_entry]
        )

        # Update session cache
        cache_manager.update_session_cache(
            {
                "session-1": SessionCacheData(
                    session_id="session-1",
                    summary="Test session",
                    first_timestamp="2024-01-01T10:00:00Z",
                    last_timestamp="2024-01-01T10:01:00Z",
                    message_count=2,
                    first_user_message="Hello, world!",
                    cwd="/test/path",
                    total_input_tokens=100,
                    total_output_tokens=50,
                )
            }
        )

        # Get project ID
        project_id = cache_manager._project_id

        # Verify data exists
        with cache_manager._get_connection() as conn:
            files = conn.execute(
                "SELECT COUNT(*) FROM cached_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            messages = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]

        assert files > 0
        assert messages > 0
        assert sessions > 0

        # Delete the project
        with cache_manager._get_connection() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()

        # Verify cascade delete removed all nested records
        with cache_manager._get_connection() as conn:
            files = conn.execute(
                "SELECT COUNT(*) FROM cached_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            messages = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]

        assert files == 0
        assert messages == 0
        assert sessions == 0


class TestTokenSumVerification:
    """Tests for token sum calculations."""

    def test_session_token_totals_match_message_sums(
        self, isolated_cache_dir, isolated_db_path, sample_assistant_entry
    ):
        """Session token totals equal sum of message tokens."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create multiple assistant entries with known token values
        entries = []
        total_input = 0
        total_output = 0

        for i in range(5):
            entry = AssistantTranscriptEntry(
                type="assistant",
                uuid=f"assistant-{i}",
                timestamp=f"2024-01-01T10:{i:02d}:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="assistant",
                cwd="/test/path",
                requestId=f"req-{i}",
                message=AssistantMessageModel(
                    id=f"msg-{i}",
                    type="message",
                    role="assistant",
                    model="claude-3",
                    content=[TextContent(type="text", text=f"Response {i}")],
                    usage=UsageInfo(
                        input_tokens=100 + i * 10,
                        output_tokens=50 + i * 5,
                    ),
                ),
            )
            entries.append(entry)
            total_input += 100 + i * 10
            total_output += 50 + i * 5

        # Save entries
        jsonl_file = isolated_cache_dir / "test.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Query actual sums from database
        with cache_manager._get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) as total_input,
                    COALESCE(SUM(output_tokens), 0) as total_output
                FROM messages
                WHERE project_id = ? AND session_id = 'session-1'
                """,
                (cache_manager._project_id,),
            ).fetchone()

        assert row["total_input"] == total_input
        assert row["total_output"] == total_output


class TestForeignKeyConstraints:
    """Tests for foreign key constraint enforcement."""

    def test_cannot_insert_message_without_valid_file_id(self, cache_manager):
        """Foreign key prevents orphaned messages."""
        with cache_manager._get_connection() as conn:
            # Attempt to insert message with non-existent file_id
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO messages (project_id, file_id, type, content)
                    VALUES (?, 99999, 'user', '{}')
                    """,
                    (cache_manager._project_id,),
                )

    def test_cannot_insert_message_without_valid_project_id(self, cache_manager):
        """Foreign key prevents messages with invalid project."""
        with cache_manager._get_connection() as conn:
            # First create a valid file
            conn.execute(
                """
                INSERT INTO cached_files (project_id, file_name, file_path, source_mtime, cached_mtime)
                VALUES (?, 'test.jsonl', '/test/test.jsonl', 0, 0)
                """,
                (cache_manager._project_id,),
            )
            file_id = conn.execute(
                "SELECT id FROM cached_files WHERE file_name = 'test.jsonl'"
            ).fetchone()[0]

            # Attempt to insert message with non-existent project_id
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO messages (project_id, file_id, type, content)
                    VALUES (99999, ?, 'user', '{}')
                    """,
                    (file_id,),
                )


class TestSerializationRoundTrip:
    """Tests for message serialization/deserialization."""

    def test_complex_message_types_roundtrip_correctly(
        self, isolated_cache_dir, isolated_db_path
    ):
        """Tool use, images, thinking content survive JSON serialization."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create entries with complex content types
        entries = [
            # Tool use
            AssistantTranscriptEntry(
                type="assistant",
                uuid="tool-use-msg",
                timestamp="2024-01-01T10:00:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="assistant",
                cwd="/test",
                requestId="req-1",
                message=AssistantMessageModel(
                    id="msg-tool",
                    type="message",
                    role="assistant",
                    model="claude-3",
                    content=[
                        ToolUseContent(
                            type="tool_use",
                            id="tool-123",
                            name="read_file",
                            input={"path": "/test/file.txt"},
                        )
                    ],
                ),
            ),
            # Tool result
            UserTranscriptEntry(
                type="user",
                uuid="tool-result-msg",
                timestamp="2024-01-01T10:01:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid="tool-use-msg",
                isSidechain=False,
                userType="tool_result",
                cwd="/test",
                message=UserMessageModel(
                    role="user",
                    content=[
                        ToolResultContent(
                            type="tool_result",
                            tool_use_id="tool-123",
                            content="File contents here",
                        )
                    ],
                ),
            ),
            # Thinking content
            AssistantTranscriptEntry(
                type="assistant",
                uuid="thinking-msg",
                timestamp="2024-01-01T10:02:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="assistant",
                cwd="/test",
                requestId="req-2",
                message=AssistantMessageModel(
                    id="msg-thinking",
                    type="message",
                    role="assistant",
                    model="claude-3",
                    content=[
                        ThinkingContent(
                            type="thinking",
                            thinking="Let me think about this...",
                        ),
                        TextContent(type="text", text="Here's my answer"),
                    ],
                ),
            ),
        ]

        # Save entries
        jsonl_file = isolated_cache_dir / "complex.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Load and compare
        loaded = cache_manager.load_cached_entries(jsonl_file)
        assert loaded is not None
        assert len(loaded) == len(entries)

        for original, loaded_entry in zip(entries, loaded):
            # Compare key fields - exact serialization may differ due to default values
            assert original.type == loaded_entry.type
            assert original.uuid == loaded_entry.uuid
            assert original.timestamp == loaded_entry.timestamp
            assert original.sessionId == loaded_entry.sessionId

            # For assistant entries, verify message content types are preserved
            if hasattr(original, "message") and hasattr(original.message, "content"):
                orig_content = original.message.content
                loaded_content = loaded_entry.message.content
                assert len(orig_content) == len(loaded_content)
                for orig_item, loaded_item in zip(orig_content, loaded_content):
                    assert orig_item.type == loaded_item.type


class TestIndexUniquenessConstraints:
    """Tests for UNIQUE constraints on indexes."""

    def test_duplicate_file_name_in_project_fails(self, cache_manager):
        """UNIQUE(project_id, file_name) enforced."""
        with cache_manager._get_connection() as conn:
            # Insert first file
            conn.execute(
                """
                INSERT INTO cached_files (project_id, file_name, file_path, source_mtime, cached_mtime)
                VALUES (?, 'duplicate.jsonl', '/path1', 0, 0)
                """,
                (cache_manager._project_id,),
            )
            conn.commit()

            # Attempt to insert duplicate file name
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO cached_files (project_id, file_name, file_path, source_mtime, cached_mtime)
                    VALUES (?, 'duplicate.jsonl', '/path2', 0, 0)
                    """,
                    (cache_manager._project_id,),
                )

    def test_duplicate_session_id_in_project_fails(self, cache_manager):
        """UNIQUE(project_id, session_id) enforced."""
        with cache_manager._get_connection() as conn:
            # Insert first session
            conn.execute(
                """
                INSERT INTO sessions (project_id, session_id, first_timestamp, last_timestamp)
                VALUES (?, 'dup-session', '2024-01-01', '2024-01-01')
                """,
                (cache_manager._project_id,),
            )
            conn.commit()

            # Attempt to insert duplicate session_id
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO sessions (project_id, session_id, first_timestamp, last_timestamp)
                    VALUES (?, 'dup-session', '2024-01-02', '2024-01-02')
                    """,
                    (cache_manager._project_id,),
                )


class TestTimestampOrdering:
    """Tests for message timestamp ordering."""

    def test_messages_ordered_by_timestamp(
        self, isolated_cache_dir, isolated_db_path, sample_user_entry
    ):
        """Messages retrieved in timestamp order."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create entries with out-of-order timestamps
        entries = []
        timestamps = [
            "2024-01-01T10:05:00Z",
            "2024-01-01T10:01:00Z",
            "2024-01-01T10:03:00Z",
            "2024-01-01T10:02:00Z",
            "2024-01-01T10:04:00Z",
        ]

        for i, ts in enumerate(timestamps):
            entry = UserTranscriptEntry(
                type="user",
                uuid=f"user-{i}",
                timestamp=ts,
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="/test",
                message=UserMessageModel(
                    role="user", content=[TextContent(type="text", text=f"Message {i}")]
                ),
            )
            entries.append(entry)

        jsonl_file = isolated_cache_dir / "order.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Load and verify order
        loaded = cache_manager.load_cached_entries(jsonl_file)
        assert loaded is not None

        loaded_timestamps = [
            ts for e in loaded if (ts := getattr(e, "timestamp", None)) is not None
        ]
        assert loaded_timestamps == sorted(loaded_timestamps)


class TestNullTokenHandling:
    """Tests for NULL token value handling."""

    def test_null_tokens_handled_in_aggregates(
        self, isolated_cache_dir, isolated_db_path
    ):
        """NULL token values don't corrupt sums."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create mix of entries with and without tokens
        entries = [
            # Entry with tokens
            AssistantTranscriptEntry(
                type="assistant",
                uuid="with-tokens",
                timestamp="2024-01-01T10:00:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="assistant",
                cwd="/test",
                requestId="req-1",
                message=AssistantMessageModel(
                    id="msg-1",
                    type="message",
                    role="assistant",
                    model="claude-3",
                    content=[TextContent(type="text", text="With tokens")],
                    usage=UsageInfo(input_tokens=100, output_tokens=50),
                ),
            ),
            # Entry without usage (NULL tokens)
            UserTranscriptEntry(
                type="user",
                uuid="without-tokens",
                timestamp="2024-01-01T10:01:00Z",
                sessionId="session-1",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="/test",
                message=UserMessageModel(
                    role="user", content=[TextContent(type="text", text="No tokens")]
                ),
            ),
        ]

        jsonl_file = isolated_cache_dir / "mixed.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Query sums - COALESCE should handle NULLs
        with cache_manager._get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) as total_input,
                    COALESCE(SUM(output_tokens), 0) as total_output
                FROM messages
                WHERE project_id = ?
                """,
                (cache_manager._project_id,),
            ).fetchone()

        # Should only count the entry with tokens
        assert row["total_input"] == 100
        assert row["total_output"] == 50


class TestMessageFileRelationship:
    """Tests for message-file relationships."""

    def test_cached_file_message_count_matches_actual(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
    ):
        """message_count column matches COUNT(*) FROM messages."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        entries = [sample_user_entry, sample_assistant_entry]
        jsonl_file = isolated_cache_dir / "count.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        with cache_manager._get_connection() as conn:
            # Get stored message count
            file_row = conn.execute(
                "SELECT id, message_count FROM cached_files WHERE file_name = ?",
                ("count.jsonl",),
            ).fetchone()

            # Get actual count
            actual_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE file_id = ?",
                (file_row["id"],),
            ).fetchone()[0]

        assert file_row["message_count"] == actual_count
        assert file_row["message_count"] == len(entries)


class TestWALMode:
    """Tests for WAL journal mode."""

    def test_wal_journal_mode_enabled(self, cache_manager):
        """Verify WAL mode is active."""
        with cache_manager._get_connection() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row[0] == "wal"

    def test_synchronous_normal(self, cache_manager):
        """Connections run with synchronous=NORMAL (1).

        Paired with WAL this skips an fsync on every commit while keeping
        durability across application crashes (only a power/OS crash can lose
        the last committed transaction). The cache is fully regenerable from
        the JSONL source, so that residual risk is acceptable and gives a large
        speedup on the per-commit fsync-bound build path (Windows especially).
        """
        with cache_manager._get_connection() as conn:
            row = conn.execute("PRAGMA synchronous").fetchone()
            # 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
            assert row[0] == 1, f"expected synchronous=NORMAL (1), got {row[0]}"


class TestConcurrentAccess:
    """Tests for concurrent database access."""

    def test_concurrent_readers_dont_block(self, isolated_cache_dir, isolated_db_path):
        """Multiple readers can access simultaneously."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Add some data
        entry = UserTranscriptEntry(
            type="user",
            uuid="user-1",
            timestamp="2024-01-01T10:00:00Z",
            sessionId="session-1",
            version="1.0.0",
            parentUuid=None,
            isSidechain=False,
            userType="external",
            cwd="/test",
            message=UserMessageModel(
                role="user", content=[TextContent(type="text", text="Test")]
            ),
        )

        jsonl_file = isolated_cache_dir / "concurrent.jsonl"
        jsonl_file.write_text(json.dumps(entry.model_dump()), encoding="utf-8")
        cache_manager.save_cached_entries(jsonl_file, [entry])

        results = []
        errors = []

        def read_data():
            try:
                cm = CacheManager(isolated_cache_dir, "1.0.0", db_path=isolated_db_path)
                data = cm.get_cached_project_data()
                results.append(data is not None)
            except Exception as e:
                errors.append(str(e))

        # Start multiple reader threads
        threads = [threading.Thread(target=read_data) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert all(results), "Not all reads succeeded"


class TestBatchConnectionReuse:
    """Tests for the batch() connection-reuse context manager.

    batch() collapses the ~190 per-call connection opens of a full build
    into one shared connection. The lifecycle guarantees are the risky part
    (Windows file locks), so they are pinned here explicitly.
    """

    def _entry(self, session_id: str = "session-1") -> UserTranscriptEntry:
        return UserTranscriptEntry(
            type="user",
            uuid="user-1",
            timestamp="2024-01-01T10:00:00Z",
            sessionId=session_id,
            version="1.0.0",
            parentUuid=None,
            isSidechain=False,
            userType="external",
            cwd="/test",
            message=UserMessageModel(
                role="user", content=[TextContent(type="text", text="Test")]
            ),
        )

    def test_default_opens_fresh_connection_per_call(self, cache_manager):
        """Outside a batch the behaviour is unchanged: every _get_connection
        opens (and closes) its own connection."""
        assert cache_manager._shared_conn is None
        with cache_manager._get_connection() as c1:
            pass
        with cache_manager._get_connection() as c2:
            pass
        assert c1 is not c2
        # Each was closed on exit (default path).
        with pytest.raises(sqlite3.ProgrammingError):
            c1.execute("SELECT 1")
        assert cache_manager._shared_conn is None

    def test_batch_reuses_single_connection(self, cache_manager):
        """Inside a batch every _get_connection yields the same connection."""
        with cache_manager.batch():
            shared = cache_manager._shared_conn
            assert shared is not None
            with cache_manager._get_connection() as c1:
                pass
            with cache_manager._get_connection() as c2:
                pass
            assert c1 is shared and c2 is shared
            # Reused connections are NOT closed when the inner `with` exits.
            c1.execute("SELECT 1")
        # Closed and cleared once the batch exits.
        assert cache_manager._shared_conn is None
        with pytest.raises(sqlite3.ProgrammingError):
            shared.execute("SELECT 1")

    def test_batch_closes_connection_on_normal_exit_allows_rmtree(self, tmp_path):
        """After a batch completes, the cache files are unlocked so the
        containing directory can be removed — the Windows WinError 32 guard."""
        cache_dir = tmp_path / "proj"
        cache_dir.mkdir()
        db_path = tmp_path / "cache.db"
        cm = CacheManager(cache_dir, "1.0.0", db_path=db_path)

        entry = self._entry()
        jsonl = cache_dir / "s.jsonl"
        jsonl.write_text(json.dumps(entry.model_dump()), encoding="utf-8")
        with cm.batch():
            cm.save_cached_entries(jsonl, [entry])

        # No lingering open handle: deleting the tree (and the db/-wal/-shm
        # files) must not raise. On Windows an open connection here would
        # raise PermissionError (WinError 32).
        shutil.rmtree(tmp_path)
        assert not tmp_path.exists()

    def test_batch_closes_connection_on_exception(self, cache_manager):
        """A build that raises inside the batch must still release the shared
        connection (finally), or a crashed build would leak the file lock."""
        captured = {}
        with pytest.raises(ValueError):
            with cache_manager.batch():
                captured["conn"] = cache_manager._shared_conn
                raise ValueError("boom")
        assert cache_manager._shared_conn is None
        with pytest.raises(sqlite3.ProgrammingError):
            captured["conn"].execute("SELECT 1")

    def test_nested_batch_is_noop_reuse(self, cache_manager):
        """A nested batch reuses the outer connection and does NOT close it on
        inner exit, so the converter loop can't double-open or close a
        connection still in use by an enclosing scope."""
        with cache_manager.batch():
            outer = cache_manager._shared_conn
            assert outer is not None
            with cache_manager.batch():
                assert cache_manager._shared_conn is outer
            # Inner exit must leave the outer connection open and active.
            assert cache_manager._shared_conn is outer
            outer.execute("SELECT 1")
        # Only the outermost batch closes it.
        assert cache_manager._shared_conn is None
        with pytest.raises(sqlite3.ProgrammingError):
            outer.execute("SELECT 1")

    def test_writes_within_batch_persist(self, tmp_path):
        """Data written through the shared connection is committed and visible
        after the batch — reuse must not drop the writes."""
        cache_dir = tmp_path / "proj"
        cache_dir.mkdir()
        cm = CacheManager(cache_dir, "1.0.0", db_path=tmp_path / "cache.db")
        entry = self._entry()
        jsonl = cache_dir / "s.jsonl"
        jsonl.write_text(json.dumps(entry.model_dump()), encoding="utf-8")
        with cm.batch():
            cm.save_cached_entries(jsonl, [entry])
        loaded = cm.load_cached_entries(jsonl)
        assert loaded is not None
        assert len(loaded) == 1

    def test_failed_configure_closes_connection(self, tmp_path, monkeypatch):
        """If a PRAGMA fails during connection setup, the just-opened handle
        must be closed (not leaked), in BOTH the per-call and batch() paths —
        otherwise the failed connection would lock the cache files. batch()
        must also not publish a half-initialised handle to _shared_conn.
        """
        import claude_code_log.cache as cachemod

        cache_dir = tmp_path / "proj"
        cache_dir.mkdir()
        cm = CacheManager(cache_dir, "1.0.0", db_path=tmp_path / "cache.db")

        # Track every connection opened after setup so we can assert it closed.
        opened: list[sqlite3.Connection] = []
        real_connect = cachemod.sqlite3.connect

        def tracking_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def boom(_conn):
            raise RuntimeError("pragma boom")

        monkeypatch.setattr(cachemod.sqlite3, "connect", tracking_connect)
        monkeypatch.setattr(cm, "_configure_connection", boom)

        # Per-call path: configure fails -> connection opened then closed.
        with pytest.raises(RuntimeError):
            with cm._get_connection():
                pass
        assert opened, "expected a connection to have been opened"
        with pytest.raises(sqlite3.ProgrammingError):
            opened[-1].execute("SELECT 1")

        # batch() path: configure fails -> connection closed AND _shared_conn
        # never left pointing at the dead handle.
        with pytest.raises(RuntimeError):
            with cm.batch():
                pass
        assert cm._shared_conn is None
        with pytest.raises(sqlite3.ProgrammingError):
            opened[-1].execute("SELECT 1")

        # No leaked OS handle: the cache dir is removable (Windows WinError 32).
        monkeypatch.undo()
        shutil.rmtree(tmp_path)
        assert not tmp_path.exists()


class TestLargeDatasetPerformance:
    """Tests for performance with large datasets."""

    def test_query_performance_with_large_dataset(
        self, isolated_cache_dir, isolated_db_path
    ):
        """Queries complete in reasonable time with large datasets."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create 1000 entries (reduced from 10k for test speed)
        entries = []
        for i in range(1000):
            entry = UserTranscriptEntry(
                type="user",
                uuid=f"user-{i}",
                timestamp=f"2024-01-{(i % 30) + 1:02d}T{i % 24:02d}:00:00Z",
                sessionId=f"session-{i % 10}",
                version="1.0.0",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="/test",
                message=UserMessageModel(
                    role="user", content=[TextContent(type="text", text=f"Message {i}")]
                ),
            )
            entries.append(entry)

        jsonl_file = isolated_cache_dir / "large.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Time filtered loading
        start = time.monotonic()
        loaded = cache_manager.load_cached_entries_filtered(
            jsonl_file, "2024-01-15", "2024-01-20"
        )
        elapsed = time.monotonic() - start

        assert loaded is not None
        assert elapsed < 2.0, f"Query took too long: {elapsed:.2f}s"


class TestSessionBoundaryDetection:
    """Tests for session boundary correctness."""

    def test_sessions_contain_correct_messages(
        self, isolated_cache_dir, isolated_db_path
    ):
        """Each session contains only its messages."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create entries for multiple sessions
        entries = []
        for session_num in range(3):
            for msg_num in range(5):
                entry = UserTranscriptEntry(
                    type="user",
                    uuid=f"user-s{session_num}-m{msg_num}",
                    timestamp=f"2024-01-01T{10 + session_num}:{msg_num * 10:02d}:00Z",
                    sessionId=f"session-{session_num}",
                    version="1.0.0",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="/test",
                    message=UserMessageModel(
                        role="user",
                        content=[
                            TextContent(
                                type="text",
                                text=f"Session {session_num} message {msg_num}",
                            )
                        ],
                    ),
                )
                entries.append(entry)

        jsonl_file = isolated_cache_dir / "sessions.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Verify each session has exactly 5 messages
        with cache_manager._get_connection() as conn:
            for session_num in range(3):
                count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE project_id = ? AND session_id = ?",
                    (cache_manager._project_id, f"session-{session_num}"),
                ).fetchone()[0]
                assert count == 5, (
                    f"Session {session_num} has {count} messages, expected 5"
                )


class TestCacheStatsAccuracy:
    """Tests for cache statistics accuracy."""

    def test_cache_stats_match_actual_counts(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
    ):
        """get_cache_stats() returns accurate data."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        entries = [sample_user_entry, sample_assistant_entry]
        jsonl_file = isolated_cache_dir / "stats.jsonl"
        jsonl_file.write_text(
            "\n".join(json.dumps(e.model_dump()) for e in entries),
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Update aggregates
        cache_manager.update_project_aggregates(
            total_message_count=2,
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=10,
            total_cache_read_tokens=5,
            earliest_timestamp="2024-01-01T10:00:00Z",
            latest_timestamp="2024-01-01T10:01:00Z",
        )

        cache_manager.update_session_cache(
            {
                "session-1": SessionCacheData(
                    session_id="session-1",
                    summary=None,
                    first_timestamp="2024-01-01T10:00:00Z",
                    last_timestamp="2024-01-01T10:01:00Z",
                    message_count=2,
                    first_user_message="Hello, world!",
                )
            }
        )

        stats = cache_manager.get_cache_stats()

        assert stats["cache_enabled"] is True
        assert stats["cached_files_count"] == 1
        assert stats["total_cached_messages"] == 2
        assert stats["total_sessions"] == 1


class TestWorkingDirectoryQuery:
    """Tests for working directory queries."""

    def test_get_working_directories_returns_distinct_cwds(
        self, isolated_cache_dir, isolated_db_path
    ):
        """get_working_directories() returns unique values."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        # Create sessions with duplicate cwds
        cache_manager.update_session_cache(
            {
                "session-1": SessionCacheData(
                    session_id="session-1",
                    summary=None,
                    first_timestamp="2024-01-01T10:00:00Z",
                    last_timestamp="2024-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    cwd="/path/to/project",
                ),
                "session-2": SessionCacheData(
                    session_id="session-2",
                    summary=None,
                    first_timestamp="2024-01-02T10:00:00Z",
                    last_timestamp="2024-01-02T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    cwd="/path/to/project",  # Same cwd
                ),
                "session-3": SessionCacheData(
                    session_id="session-3",
                    summary=None,
                    first_timestamp="2024-01-03T10:00:00Z",
                    last_timestamp="2024-01-03T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    cwd="/different/path",
                ),
            }
        )

        cwds = cache_manager.get_working_directories()

        # Should be deduplicated
        assert len(cwds) == 2
        assert set(cwds) == {"/path/to/project", "/different/path"}


class TestFileModificationDetection:
    """Tests for file modification time detection."""

    def test_mtime_change_invalidates_cache(
        self, isolated_cache_dir, isolated_db_path, sample_user_entry
    ):
        """Changing file mtime marks cache as stale."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        jsonl_file = isolated_cache_dir / "mtime.jsonl"
        jsonl_file.write_text(
            json.dumps(sample_user_entry.model_dump()), encoding="utf-8"
        )
        cache_manager.save_cached_entries(jsonl_file, [sample_user_entry])

        # Verify cache is valid
        assert cache_manager.is_file_cached(jsonl_file) is True

        # Rewrite the file and make it *look* newer than the cached mtime.
        # See bump_mtime in conftest: sleeping past the 1.0 s threshold is
        # not reliable, and this states the intent directly.
        jsonl_file.write_text(
            json.dumps(sample_user_entry.model_dump()) + "\n", encoding="utf-8"
        )
        bump_mtime(jsonl_file)

        # Cache should be invalidated
        assert cache_manager.is_file_cached(jsonl_file) is False


class TestMigrationIntegrity:
    """Tests for migration system integrity."""

    def test_migration_checksum_stored(self, isolated_cache_dir, isolated_db_path):
        """Migration checksums are stored in _schema_version."""
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        with cache_manager._get_connection() as conn:
            rows = conn.execute(
                "SELECT version, filename, checksum FROM _schema_version"
            ).fetchall()

        assert len(rows) >= 1
        for row in rows:
            assert row["version"] > 0
            assert row["filename"].endswith(".sql")
            assert len(row["checksum"]) == 64  # SHA256 hex length

    def test_migration_applied_only_once(self, isolated_cache_dir, isolated_db_path):
        """Migrations are not re-applied on subsequent runs."""
        # First run
        cm1 = CacheManager(isolated_cache_dir, "1.0.0", db_path=isolated_db_path)

        with cm1._get_connection() as conn:
            initial_count = conn.execute(
                "SELECT COUNT(*) FROM _schema_version"
            ).fetchone()[0]

        # Second run
        cm2 = CacheManager(isolated_cache_dir, "1.0.0", db_path=isolated_db_path)

        with cm2._get_connection() as conn:
            final_count = conn.execute(
                "SELECT COUNT(*) FROM _schema_version"
            ).fetchone()[0]

        assert initial_count == final_count


class TestCorruptDatabaseRecovery:
    """A cache file damaged past reading is discarded and rebuilt.

    Written against two real shapes. The first is the reported one: a cache
    truncated to 11.6 MB while its header still claimed 58.1 MB, on which no
    statement at all succeeded. The second is what made it *visible* —
    migration 012's `CREATE INDEX` is the first operation to full-scan
    `messages`, so damage confined to that table's leaf pages had been sitting
    unnoticed under earlier versions, which never read those pages.
    """

    @staticmethod
    def _populate(cache_dir: Path, db_path: Path, user_entry, assistant_entry) -> None:
        """Build a real, healthy cache with enough rows to damage."""
        cache_manager = CacheManager(cache_dir, "1.0.0", db_path=db_path)
        jsonl_file = cache_dir / "test.jsonl"
        jsonl_file.write_text(
            json.dumps(user_entry.model_dump())
            + "\n"
            + json.dumps(assistant_entry.model_dump())
            + "\n",
            encoding="utf-8",
        )
        cache_manager.save_cached_entries(jsonl_file, [user_entry, assistant_entry])

    def test_truncated_database_is_discarded_and_rebuilt(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
        capsys,
    ):
        """The reported failure: header claims more pages than the file holds."""
        self._populate(
            isolated_cache_dir,
            isolated_db_path,
            sample_user_entry,
            sample_assistant_entry,
        )
        original_size = isolated_db_path.stat().st_size
        with open(isolated_db_path, "r+b") as f:
            f.truncate(original_size // 4)

        # Constructing over the damaged file must succeed, not raise.
        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        assert "corrupt" in capsys.readouterr().out.lower()
        with cache_manager._get_connection() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Rebuilt empty rather than carrying damaged rows forward: the project
        # row exists again, but the messages the old file held are gone, so the
        # next conversion re-ingests them from the JSONL source.
        assert cache_manager._project_id is not None
        with cache_manager._get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM cached_files").fetchone()[0] == 0

    def test_corrupt_messages_pages_surface_and_heal_during_migration(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
        capsys,
    ):
        """Damage only a full table scan reaches still heals.

        Models the reported sequence exactly: a cache written by a version
        that lacked migration 012, damaged in `messages`, then opened by one
        that has it. The pending `CREATE INDEX` scans the damaged pages and
        raises where nothing else would.
        """
        self._populate(
            isolated_cache_dir,
            isolated_db_path,
            sample_user_entry,
            sample_assistant_entry,
        )
        # Roll the schema back to pre-012 so the index build is pending again.
        conn = sqlite3.connect(isolated_db_path)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("DELETE FROM _schema_version WHERE version >= 12")
        for name in (
            "idx_messages_project_uuid",
            "idx_messages_project_parent_uuid",
            "idx_messages_project_request_id",
            "idx_messages_project_metadata_type",
            "idx_messages_project_session_ts",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
        pages = [
            r[0]
            for r in conn.execute(
                "SELECT pageno FROM dbstat WHERE name='messages' AND pagetype='leaf'"
            )
        ]
        # `dbstat.pageno` counts pages of whatever size this database was
        # built with, so read it rather than assuming SQLite's 4096 default.
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()
        assert pages, "expected messages to occupy at least one leaf page"
        # Overwrite the whole page, not a slice of it. A page holding two small
        # rows is mostly free space, and cells are laid out from the *end*, so
        # scribbling near the start damages nothing SQLite reads.
        with open(isolated_db_path, "r+b") as f:
            for pageno in pages:
                f.seek((pageno - 1) * page_size)
                f.write(b"\xff" * page_size)
        # `_populate` left this path in the process-level "migrations already
        # checked" memo, which would skip the pending migration. Real runs meet
        # this across processes; drop the entry to stand in for a fresh one.
        _migrated_db_paths.discard(str(isolated_db_path))

        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        assert "corrupt" in capsys.readouterr().out.lower()
        with cache_manager._get_connection() as conn2:
            assert conn2.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            applied = [
                r[0] for r in conn2.execute("SELECT version FROM _schema_version")
            ]
        # The migration that failed is applied on the rebuilt database, so the
        # failure cannot repeat on every subsequent run.
        assert 12 in applied

    def test_garbage_file_is_replaced(
        self, isolated_cache_dir, isolated_db_path, capsys
    ):
        """A non-database file at the cache path is replaced, not fatal."""
        isolated_db_path.write_bytes(b"this is not a database" * 500)

        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        assert "corrupt" in capsys.readouterr().out.lower()
        assert cache_manager._project_id is not None

    def test_wal_sidecars_are_removed_with_the_database(self, isolated_db_path):
        """A stale -wal beside a fresh database is its own route to malformed."""
        isolated_db_path.write_bytes(b"x")
        wal = isolated_db_path.with_name(isolated_db_path.name + "-wal")
        shm = isolated_db_path.with_name(isolated_db_path.name + "-shm")
        wal.write_bytes(b"x")
        shm.write_bytes(b"x")

        assert discard_database_files(isolated_db_path) is True

        assert not isolated_db_path.exists()
        assert not wal.exists()
        assert not shm.exists()

    def test_empty_file_is_not_treated_as_corruption(
        self, isolated_cache_dir, isolated_db_path, capsys
    ):
        """SQLite adopts a zero-byte file as a new database; don't panic."""
        isolated_db_path.write_bytes(b"")

        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path
        )

        assert "corrupt" not in capsys.readouterr().out.lower()
        assert cache_manager._project_id is not None

    def test_read_only_manager_never_deletes_the_database(
        self,
        isolated_cache_dir,
        isolated_db_path,
        sample_user_entry,
        sample_assistant_entry,
    ):
        """Render workers open read-only and must not delete a shared file.

        Several run concurrently against one database; a worker that reacted
        to corruption by unlinking it would pull the file out from under its
        siblings. Degrading to "no cached data" is the contract.
        """
        self._populate(
            isolated_cache_dir,
            isolated_db_path,
            sample_user_entry,
            sample_assistant_entry,
        )
        with open(isolated_db_path, "r+b") as f:
            f.truncate(isolated_db_path.stat().st_size // 4)
        size_before = isolated_db_path.stat().st_size

        cache_manager = CacheManager(
            isolated_cache_dir, "1.0.0", db_path=isolated_db_path, read_only=True
        )

        assert cache_manager._project_id is None
        assert isolated_db_path.exists()
        assert isolated_db_path.stat().st_size == size_before

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (sqlite3.DatabaseError("database disk image is malformed"), True),
            (sqlite3.DatabaseError("file is not a database"), True),
            (sqlite3.DatabaseError("malformed database schema (message_fts)"), True),
            # Transient and environmental failures must never delete a cache
            # that is merely busy, or on a disk that is merely full.
            (sqlite3.OperationalError("database is locked"), False),
            (sqlite3.OperationalError("database or disk is full"), False),
            (sqlite3.OperationalError("no such table: messages"), False),
            (OSError("permission denied"), False),
        ],
    )
    def test_only_corruption_triggers_a_rebuild(self, exc, expected):
        assert is_corrupt_database_error(exc) is expected

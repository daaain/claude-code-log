"""Integration tests for DAG-based ordering in the rendering pipeline.

Tests that DAG ordering in load_directory_transcripts() produces correct
results when wired into the converter's directory-mode loading.
"""

import json
import logging
from pathlib import Path
from typing import Any


from claude_code_log.converter import (
    load_directory_transcripts,
    _build_session_data_from_messages,
)
from claude_code_log.models import (
    SummaryTranscriptEntry,
    QueueOperationTranscriptEntry,
)


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write entries as JSONL lines to a file."""
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _make_user_entry(
    uuid: str,
    session_id: str,
    timestamp: str,
    parent_uuid: str | None = None,
    text: str = "msg",
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Helper to create a user transcript entry dict."""
    entry: dict[str, Any] = {
        "type": "user",
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": is_sidechain,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def _make_assistant_entry(
    uuid: str,
    session_id: str,
    timestamp: str,
    parent_uuid: str | None = None,
    text: str = "reply",
    is_sidechain: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Helper to create an assistant transcript entry dict."""
    entry: dict[str, Any] = {
        "type": "assistant",
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": is_sidechain,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "requestId": f"req_{uuid}",
        "message": {
            "id": uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


# =============================================================================
# Test: DAG ordering in load_directory_transcripts
# =============================================================================


class TestLoadDirectoryDagOrdering:
    """Test that load_directory_transcripts uses DAG ordering."""

    def test_load_directory_dag_ordering(self, tmp_path: Path) -> None:
        """Split dag_resume entries across two files, verify session grouping."""
        # File 1: session s1 entries (a→b→c→d→e)
        file1_entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            _make_user_entry("c", "s1", "2025-07-01T10:02:00.000Z", "b"),
            _make_assistant_entry("d", "s1", "2025-07-01T10:03:00.000Z", "c"),
            _make_user_entry("e", "s1", "2025-07-01T10:04:00.000Z", "d"),
        ]
        # File 2: session s2 entries (f→g→h, f.parent=e)
        file2_entries = [
            _make_user_entry("f", "s2", "2025-07-01T11:00:00.000Z", "e", "Resume"),
            _make_assistant_entry("g", "s2", "2025-07-01T11:01:00.000Z", "f"),
            _make_user_entry("h", "s2", "2025-07-01T11:02:00.000Z", "g"),
        ]

        _write_jsonl(tmp_path / "session1.jsonl", file1_entries)
        _write_jsonl(tmp_path / "session2.jsonl", file2_entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)

        # Should have all 8 entries in DAG order (s1 then s2)
        uuids = [getattr(e, "uuid", None) for e in result]
        assert uuids == ["a", "b", "c", "d", "e", "f", "g", "h"]

    def test_load_directory_with_sidechains(self, tmp_path: Path) -> None:
        """Sidechain entries are integrated into DAG at their structural position."""
        main_entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
        ]
        sidechain_entries = [
            _make_user_entry(
                "sc1",
                "s1",
                "2025-07-01T10:00:30.000Z",
                "a",
                "Sidechain msg",
                is_sidechain=True,
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + sidechain_entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)
        uuids = [getattr(e, "uuid", None) for e in result]

        # Sidechain is now part of the DAG: sc1 is a child of a (tool-result
        # side-branch), stitched before the continuation child b
        assert uuids == ["a", "sc1", "b"]

    def test_load_directory_with_summaries(self, tmp_path: Path) -> None:
        """Summary entries should be preserved in output."""
        entries: list[dict[str, Any]] = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            {
                "type": "summary",
                "summary": "A test summary",
                "leafUuid": "b",
                "timestamp": "2025-07-01T10:05:00.000Z",
            },
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)

        # Should have 2 DAG entries + 1 summary
        summary_entries = [e for e in result if isinstance(e, SummaryTranscriptEntry)]
        assert len(summary_entries) == 1
        assert summary_entries[0].summary == "A test summary"

        # DAG entries should be present
        dag_uuids = [
            getattr(e, "uuid", None)
            for e in result
            if not isinstance(e, SummaryTranscriptEntry)
        ]
        assert "a" in dag_uuids
        assert "b" in dag_uuids

    def test_load_directory_degenerate_parentuuid(self, tmp_path: Path) -> None:
        """All parentUuid=null entries should be returned, none lost."""
        entries = [
            _make_user_entry(
                f"msg_{i}", "s1", f"2025-07-01T10:0{i}:00.000Z", None, f"Msg {i}"
            )
            for i in range(5)
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)

        # All 5 entries should be present
        uuids = [getattr(e, "uuid", None) for e in result]
        assert len(uuids) == 5
        # Should be timestamp-ordered (fallback)
        assert uuids == ["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"]

    def test_load_directory_with_queue_operations(self, tmp_path: Path) -> None:
        """Queue operation entries should be preserved in output."""
        entries: list[dict[str, Any]] = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            {
                "type": "queue-operation",
                "operation": "dequeue",
                "timestamp": "2025-07-01T10:00:30.000Z",
                "sessionId": "s1",
            },
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)

        queue_entries = [
            e for e in result if isinstance(e, QueueOperationTranscriptEntry)
        ]
        assert len(queue_entries) == 1
        assert queue_entries[0].operation == "dequeue"


# =============================================================================
# Test: End-to-end with generate_template_messages
# =============================================================================


class TestEndToEndHtmlWithDag:
    """Test full pipeline from DAG-ordered directory input to template messages."""

    def test_end_to_end_html_with_dag(self, tmp_path: Path) -> None:
        """Full generate_template_messages() with DAG-ordered directory input."""
        from claude_code_log.renderer import generate_template_messages

        # Two sessions: s1 (earlier), s2 (later, resumes from s1)
        file1_entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Hello"),
            _make_assistant_entry(
                "b", "s1", "2025-07-01T10:01:00.000Z", "a", "Hi there"
            ),
        ]
        file2_entries = [
            _make_user_entry("c", "s2", "2025-07-01T11:00:00.000Z", "b", "Resume"),
            _make_assistant_entry(
                "d", "s2", "2025-07-01T11:01:00.000Z", "c", "Resumed"
            ),
        ]

        _write_jsonl(tmp_path / "session1.jsonl", file1_entries)
        _write_jsonl(tmp_path / "session2.jsonl", file2_entries)

        # Load via DAG ordering
        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)

        # Run through template message generation (reuse pre-built tree)
        root_messages, session_nav, context = generate_template_messages(
            messages, session_tree=session_tree
        )

        # Extract session headers and verify order
        from claude_code_log.models import SessionHeaderMessage

        session_headers = [
            tm for tm in root_messages if isinstance(tm.content, SessionHeaderMessage)
        ]

        # Should have session headers
        assert len(session_headers) >= 2
        # s1 header should come before s2 header
        s1_idx = next(
            i
            for i, tm in enumerate(root_messages)
            if isinstance(tm.content, SessionHeaderMessage)
            and tm.content.session_id == "s1"
        )
        s2_idx = next(
            i
            for i, tm in enumerate(root_messages)
            if isinstance(tm.content, SessionHeaderMessage)
            and tm.content.session_id == "s2"
        )
        assert s1_idx < s2_idx

    def test_session_header_hierarchy_fields(self, tmp_path: Path) -> None:
        """Session headers carry hierarchy fields (parent, depth) from DAG."""
        from claude_code_log.renderer import generate_template_messages
        from claude_code_log.models import SessionHeaderMessage

        # s1 is root, s2 resumes from s1 (c.parent=b), s3 resumes from s2
        entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            _make_user_entry("c", "s2", "2025-07-01T11:00:00.000Z", "b", "Resume"),
            _make_assistant_entry("d", "s2", "2025-07-01T11:01:00.000Z", "c"),
            _make_user_entry("e", "s3", "2025-07-01T12:00:00.000Z", "d", "Resume2"),
            _make_assistant_entry("f", "s3", "2025-07-01T12:01:00.000Z", "e"),
        ]

        _write_jsonl(tmp_path / "sessions.jsonl", entries)
        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        root_messages, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        headers = {
            tm.content.session_id: tm.content
            for tm in root_messages
            if isinstance(tm.content, SessionHeaderMessage)
        }

        # s1 is root
        assert headers["s1"].parent_session_id is None
        assert headers["s1"].depth == 0

        # s2 is child of s1
        assert headers["s2"].parent_session_id == "s1"
        assert headers["s2"].depth == 1

        # s3 is grandchild (child of s2)
        assert headers["s3"].parent_session_id == "s2"
        assert headers["s3"].depth == 2

    def test_session_nav_hierarchy_fields(self, tmp_path: Path) -> None:
        """Session nav entries carry hierarchy data for template rendering."""
        from claude_code_log.renderer import generate_template_messages

        entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            _make_user_entry("c", "s2", "2025-07-01T11:00:00.000Z", "b", "Resume"),
            _make_assistant_entry("d", "s2", "2025-07-01T11:01:00.000Z", "c"),
        ]

        _write_jsonl(tmp_path / "sessions.jsonl", entries)
        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _root, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        nav_by_id = {s["id"]: s for s in session_nav}

        # s1 root
        assert nav_by_id["s1"]["parent_session_id"] is None
        assert nav_by_id["s1"]["depth"] == 0

        # s2 child
        assert nav_by_id["s2"]["parent_session_id"] == "s1"
        assert nav_by_id["s2"]["depth"] == 1

    def test_degenerate_data_no_hierarchy(self, tmp_path: Path) -> None:
        """Degenerate data (all null parentUuid) has no hierarchy."""
        from claude_code_log.renderer import generate_template_messages
        from claude_code_log.models import SessionHeaderMessage

        entries = [
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", None, "Msg"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", None),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)
        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        root_messages, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        header = next(
            tm.content
            for tm in root_messages
            if isinstance(tm.content, SessionHeaderMessage)
        )
        assert header.parent_session_id is None
        assert header.depth == 0

        assert session_nav[0]["depth"] == 0
        assert session_nav[0]["parent_session_id"] is None

    def test_end_to_end_degenerate_data_matches_timestamp_sort(
        self, tmp_path: Path
    ) -> None:
        """Degenerate data (all null parentUuid) produces same order as timestamp sort."""
        from claude_code_log.renderer import generate_template_messages

        # 3 entries with parentUuid=null
        entries = [
            _make_user_entry("x", "s1", "2025-07-01T10:00:00.000Z", None, "First"),
            _make_assistant_entry("y", "s1", "2025-07-01T10:01:00.000Z", None, "Mid"),
            _make_user_entry("z", "s1", "2025-07-01T10:02:00.000Z", None, "Last"),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)

        # Verify order matches timestamps
        uuids = [getattr(e, "uuid", None) for e in messages if hasattr(e, "uuid")]
        assert uuids == ["x", "y", "z"]

        # Also verify template generation works (reuse pre-built tree)
        root_messages, session_nav, context = generate_template_messages(
            messages, session_tree=session_tree
        )
        assert len(root_messages) > 0


# =============================================================================
# Test: Progress chain repair
# =============================================================================

# Path to real project test data with progress entries
EXPERIMENTS_IDEAS_DIR = (
    Path(__file__).parent / "test_data" / "real_projects" / "-experiments-ideas"
)


def _make_progress_entry(uuid: str, parent_uuid: str | None = None) -> dict[str, Any]:
    """Helper to create a progress transcript entry dict."""
    return {
        "type": "progress",
        "timestamp": "2025-07-01T10:00:00.000Z",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "sessionId": "s1",
        "content": {"type": "hook_progress"},
    }


class TestProgressEntryPassthrough:
    """Progress entries are bridged into the DAG as PassthroughTranscriptEntry.

    Historically a `_scan_progress_chains` / `_repair_parent_chains` pass
    rewrote parentUuids around dropped `progress` entries. Once `progress`
    entries (which carry uuid+sessionId) became `PassthroughTranscriptEntry`
    nodes, that repair became a no-op and was removed; these tests pin the
    behaviour the Passthrough mechanism now provides on its own.
    """

    def test_no_orphan_warnings_from_progress_entries(self, caplog: Any) -> None:
        """Progress entries are bridged: no orphan warnings from DAG build."""
        with caplog.at_level(logging.WARNING):
            result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)

        # Should have loaded entries (41 non-progress, non-file-history entries)
        assert len(result) > 0

        # No orphan warnings should appear in logs
        orphan_warnings = [
            r.message for r in caplog.records if "Orphan node" in r.message
        ]
        assert orphan_warnings == [], (
            f"Expected no orphan warnings, got: {orphan_warnings}"
        )

    def test_progress_entries_become_passthrough_real_data(self) -> None:
        """Real-data single file: progress entries land as Passthrough nodes
        and downstream entries still point at them (chain intact, no repair)."""
        from claude_code_log.converter import load_transcript
        from claude_code_log.models import PassthroughTranscriptEntry

        single_file = (
            EXPERIMENTS_IDEAS_DIR / "03eb5929-52b3-4b13-ada3-b93ae35806b8.jsonl"
        )
        messages = load_transcript(single_file, silent=True)

        # Progress entries are present in the messages list as Passthrough nodes
        progress_uuids = {
            m.uuid
            for m in messages
            if isinstance(m, PassthroughTranscriptEntry) and m.type == "progress"
        }
        assert len(progress_uuids) > 0

        # Entries point at those progress UUIDs — valid DAG nodes, no repair needed
        entries_with_progress_parents = sum(
            1 for m in messages if getattr(m, "parentUuid", None) in progress_uuids
        )
        assert entries_with_progress_parents > 0

    def test_dag_chain_fully_connected(self) -> None:
        """DAG build over real data produces a connected chain."""
        from claude_code_log.dag import build_dag_from_entries

        result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)

        # Filter to DAG-eligible entries (those with uuid)
        dag_entries = [e for e in result if hasattr(e, "uuid")]

        # Build DAG to verify connectivity
        tree = build_dag_from_entries(dag_entries)

        # Should have sessions in the tree
        assert len(tree.sessions) >= 1

        # All DAG-eligible entries should be accounted for in the tree nodes
        assert len(tree.nodes) > 0

    def test_synthetic_progress_in_directory_mode(self, tmp_path: Path) -> None:
        """Progress entries become passthrough nodes in directory mode."""
        from claude_code_log.models import PassthroughTranscriptEntry

        entries: list[dict[str, Any]] = [
            _make_progress_entry("p1", None),
            _make_user_entry("a", "s1", "2025-07-01T10:00:00.000Z", "p1", "Start"),
            _make_assistant_entry("b", "s1", "2025-07-01T10:01:00.000Z", "a"),
            _make_progress_entry("p2", "b"),
            _make_user_entry("c", "s1", "2025-07-01T10:02:00.000Z", "p2", "Continue"),
            _make_assistant_entry("d", "s1", "2025-07-01T10:03:00.000Z", "c"),
        ]
        _write_jsonl(tmp_path / "session.jsonl", entries)

        result, _ = load_directory_transcripts(tmp_path, silent=True)
        uuids = [getattr(e, "uuid", None) for e in result]

        # All 6 entries in DAG order (including passthrough progress entries)
        assert uuids == ["p1", "a", "b", "p2", "c", "d"]

        # Progress entries are PassthroughTranscriptEntry
        passthrough = [e for e in result if isinstance(e, PassthroughTranscriptEntry)]
        assert len(passthrough) == 2
        assert {p.uuid for p in passthrough} == {"p1", "p2"}


# =============================================================================
# Test: Within-session fork detection in real data
# =============================================================================


class TestWithinSessionForkRealData:
    """Test fork detection using real session 03eb5929 which has a fork at eb84."""

    def test_fork_detected_at_eb84(self) -> None:
        """The real data has a fork at eb84 with two children (5270, 9edc)."""
        from claude_code_log.dag import build_dag_from_entries

        result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)
        dag_entries = [e for e in result if hasattr(e, "uuid")]
        tree = build_dag_from_entries(dag_entries)

        # Find the fork junction at eb84
        fork_jps = [
            (uuid, jp)
            for uuid, jp in tree.junction_points.items()
            if uuid.startswith("eb84") and any("@" in t for t in jp.target_sessions)
        ]
        assert len(fork_jps) == 1, (
            f"Expected 1 fork junction at eb84, got {len(fork_jps)}"
        )
        uuid, jp = fork_jps[0]
        assert len(jp.target_sessions) == 2

    def test_no_linearity_warnings(self, caplog: Any) -> None:
        """Fork handling should produce no linearity violation warnings."""
        import logging
        from claude_code_log.dag import build_dag_from_entries

        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)
            dag_entries = [e for e in result if hasattr(e, "uuid")]
            build_dag_from_entries(dag_entries)

        linearity_warnings = [
            r.message for r in caplog.records if "linearity" in r.message
        ]
        assert linearity_warnings == []

    def test_branch_sessions_created(self) -> None:
        """Branch pseudo-sessions are created for the fork."""
        from claude_code_log.dag import build_dag_from_entries

        result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)
        dag_entries = [e for e in result if hasattr(e, "uuid")]
        tree = build_dag_from_entries(dag_entries)

        branch_sessions = [sid for sid in tree.sessions if "@" in sid]
        assert len(branch_sessions) >= 2

        for sid in branch_sessions:
            dl = tree.sessions[sid]
            assert dl.is_branch is True
            assert dl.original_session_id is not None
            assert len(dl.uuids) > 0

    def test_end_to_end_rendering_with_fork(self) -> None:
        """Full rendering pipeline produces branch headers for fork."""
        from claude_code_log.renderer import generate_template_messages
        from claude_code_log.models import SessionHeaderMessage

        result, session_tree = load_directory_transcripts(
            EXPERIMENTS_IDEAS_DIR, silent=True
        )
        root_messages, session_nav, ctx = generate_template_messages(
            result, session_tree=session_tree
        )

        # Branch-headers live under their parent session (ancestry 0.5),
        # so walk the full tree rather than inspecting only roots.
        def walk(msgs):
            for m in msgs:
                yield m
                yield from walk(m.children)

        branch_headers = [
            tm
            for tm in walk(root_messages)
            if isinstance(tm.content, SessionHeaderMessage) and tm.content.is_branch
        ]
        assert len(branch_headers) >= 2

    def test_within_fork_coverage(self) -> None:
        """All entries are covered by DAG-lines (trunk + branches)."""
        from claude_code_log.dag import build_dag_from_entries

        result, _ = load_directory_transcripts(EXPERIMENTS_IDEAS_DIR, silent=True)
        dag_entries = [e for e in result if hasattr(e, "uuid")]
        tree = build_dag_from_entries(dag_entries)

        # Should have both trunk and branch pseudo-sessions
        real_sessions = [sid for sid in tree.sessions if "@" not in sid]
        branch_sessions = [sid for sid in tree.sessions if "@" in sid]
        assert len(real_sessions) >= 1
        assert len(branch_sessions) >= 2

        # All entries should be covered
        total_in_daglines = sum(len(dl.uuids) for dl in tree.sessions.values())
        assert total_in_daglines == len(tree.nodes)

    def test_fork_point_rendered_inside_children_container(self) -> None:
        """Every ``.fork-point`` marker must render INSIDE a ``.children``
        container so it folds together with the subtree (CodeRabbit on
        PR #191). With the dissociated nested DOM the fold logic hides a
        node's ``.children``; a fork-point left as a *sibling* of
        ``.children`` would stay visible when the node is folded —
        orphaned branch UI. Walk the rendered DOM and assert each
        fork-point has a ``.children`` ancestor.
        """
        from pathlib import Path
        from html.parser import HTMLParser
        from claude_code_log.converter import load_transcript
        from claude_code_log.html.renderer import generate_html

        fixture = Path(__file__).parent / "test_data" / "dag_within_fork.jsonl"
        entries = load_transcript(fixture)
        html = generate_html(entries)

        class ForkWalker(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.classes_stack: list[set[str]] = []
                self.fork_total = 0
                self.fork_inside_children = 0

            def handle_starttag(self, tag, attrs):
                # Track only <div> nesting so pushes here stay balanced with
                # the <div>-only pops in handle_endtag — otherwise non-div
                # tags would leave stale ancestors on the stack and a
                # fork-point OUTSIDE .children could be miscounted as inside.
                if tag != "div":
                    return
                classes = set((dict(attrs).get("class") or "").split())
                if "fork-point" in classes:
                    self.fork_total += 1
                    if any("children" in c for c in self.classes_stack):
                        self.fork_inside_children += 1
                self.classes_stack.append(classes)

            def handle_endtag(self, tag):
                if tag == "div" and self.classes_stack:
                    self.classes_stack.pop()

        walker = ForkWalker()
        walker.feed(html)

        # The fixture has a within-session fork → at least one fork-point.
        assert walker.fork_total >= 1, "expected dag_within_fork to render a fork-point"
        # Every fork-point must be nested inside a .children container.
        assert walker.fork_inside_children == walker.fork_total, (
            f"{walker.fork_total - walker.fork_inside_children} fork-point(s) "
            f"rendered OUTSIDE a .children container (would orphan on fold)"
        )


# =============================================================================
# Test: Agent transcript DAG integration
# =============================================================================


class TestAgentDagIntegration:
    """Test that agent (sidechain) transcripts are integrated into the DAG."""

    def test_agent_entries_parented_to_anchor(self, tmp_path: Path) -> None:
        """Agent root entry gets parentUuid pointing to the anchor tool_result."""
        # Main session: user → assistant(tool_use Agent) → user(tool_result, agentId)
        main_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("a1", "s1", "2025-07-01T10:01:00.000Z", "u1"),
            # User entry carrying tool_result with agentId reference
            _make_user_entry(
                "u2",
                "s1",
                "2025-07-01T10:02:00.000Z",
                "a1",
                "tool result",
                agent_id="agent-abc",
            ),
            _make_assistant_entry("a2", "s1", "2025-07-01T10:03:00.000Z", "u2"),
        ]
        # Agent file entries (all sidechain)
        agent_entries = [
            _make_user_entry(
                "ag1",
                "s1",
                "2025-07-01T10:01:30.000Z",
                None,
                "Agent prompt",
                is_sidechain=True,
                agent_id="agent-abc",
            ),
            _make_assistant_entry(
                "ag2",
                "s1",
                "2025-07-01T10:01:40.000Z",
                "ag1",
                "Agent reply",
                is_sidechain=True,
                agent_id="agent-abc",
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + agent_entries)

        result, tree = load_directory_transcripts(tmp_path, silent=True)
        uuids = [getattr(e, "uuid", None) for e in result]

        # Agent entries should be in the DAG, placed at the junction point
        assert "ag1" in uuids
        assert "ag2" in uuids
        # Main session entries should be in order
        assert uuids.index("u1") < uuids.index("a1")
        assert uuids.index("a1") < uuids.index("u2")
        assert uuids.index("u2") < uuids.index("a2")
        # Agent entries should appear between the anchor (u2) and
        # continuation (a2) — the agent DAG-line is a child session
        # traversed at the junction point
        assert uuids.index("u2") < uuids.index("ag1")
        assert uuids.index("ag2") < uuids.index("a2")

    def test_agent_session_in_tree(self, tmp_path: Path) -> None:
        """Agent transcript creates a synthetic child session in the tree."""
        main_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_user_entry(
                "u2",
                "s1",
                "2025-07-01T10:02:00.000Z",
                "u1",
                "tool result",
                agent_id="agent-xyz",
            ),
        ]
        agent_entries = [
            _make_user_entry(
                "ag1",
                "s1",
                "2025-07-01T10:01:00.000Z",
                None,
                "Agent prompt",
                is_sidechain=True,
                agent_id="agent-xyz",
            ),
            _make_assistant_entry(
                "ag2",
                "s1",
                "2025-07-01T10:01:10.000Z",
                "ag1",
                "Agent reply",
                is_sidechain=True,
                agent_id="agent-xyz",
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + agent_entries)

        _, tree = load_directory_transcripts(tmp_path, silent=True)

        # Should have synthetic agent session
        agent_sids = [sid for sid in tree.sessions if "#agent-" in sid]
        assert len(agent_sids) == 1
        assert agent_sids[0] == "s1#agent-agent-xyz"

        # Agent session should be a child of the main session
        agent_dag_line = tree.sessions[agent_sids[0]]
        assert agent_dag_line.parent_session_id == "s1"
        assert agent_dag_line.attachment_uuid == "u2"

        # Main session should be a root
        assert "s1" in tree.roots
        assert agent_sids[0] not in tree.roots

    def test_agent_session_has_no_separate_header(self, tmp_path: Path) -> None:
        """Subagent sessions are inlined under their trunk anchor; they do
        not produce a standalone SessionHeaderMessage.

        ``_integrate_agent_entries`` still stamps subagent entries with a
        synthetic ``{main}#agent-{agent_id}`` sessionId so the DAG walker
        gives each subagent its own DAG-line. ``_render_messages`` skips
        the header for those, and ``_relocate_subagent_blocks`` splices
        each subagent's chunks back next to its anchor.
        """
        from claude_code_log.renderer import generate_template_messages
        from claude_code_log.models import SessionHeaderMessage

        main_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_user_entry(
                "u2",
                "s1",
                "2025-07-01T10:02:00.000Z",
                "u1",
                "tool result",
                agent_id="agent-xyz",
            ),
        ]
        agent_entries = [
            _make_user_entry(
                "ag1",
                "s1",
                "2025-07-01T10:01:00.000Z",
                None,
                "Agent prompt",
                is_sidechain=True,
                agent_id="agent-xyz",
            ),
            _make_assistant_entry(
                "ag2",
                "s1",
                "2025-07-01T10:01:10.000Z",
                "ag1",
                "Agent reply",
                is_sidechain=True,
                agent_id="agent-xyz",
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + agent_entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _roots, _nav, context = generate_template_messages(
            messages, session_tree=session_tree
        )

        # Only the main session header is produced; the subagent session
        # has no header of its own.
        header_session_ids = {
            m.content.session_id
            for m in context.messages
            if isinstance(m.content, SessionHeaderMessage)
        }
        assert header_session_ids == {"s1"}

    def test_multiple_agents_ordered(self, tmp_path: Path) -> None:
        """Multiple agents are each placed at their respective anchor points."""
        main_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("a1", "s1", "2025-07-01T10:01:00.000Z", "u1"),
            # First agent anchor
            _make_user_entry(
                "u2",
                "s1",
                "2025-07-01T10:02:00.000Z",
                "a1",
                "result1",
                agent_id="agent-1",
            ),
            _make_assistant_entry("a2", "s1", "2025-07-01T10:03:00.000Z", "u2"),
            # Second agent anchor
            _make_user_entry(
                "u3",
                "s1",
                "2025-07-01T10:04:00.000Z",
                "a2",
                "result2",
                agent_id="agent-2",
            ),
            _make_assistant_entry("a3", "s1", "2025-07-01T10:05:00.000Z", "u3"),
        ]
        agent1_entries = [
            _make_user_entry(
                "ag1-1",
                "s1",
                "2025-07-01T10:01:30.000Z",
                None,
                "Agent1 prompt",
                is_sidechain=True,
                agent_id="agent-1",
            ),
            _make_assistant_entry(
                "ag1-2",
                "s1",
                "2025-07-01T10:01:40.000Z",
                "ag1-1",
                "Agent1 reply",
                is_sidechain=True,
                agent_id="agent-1",
            ),
        ]
        agent2_entries = [
            _make_user_entry(
                "ag2-1",
                "s1",
                "2025-07-01T10:03:30.000Z",
                None,
                "Agent2 prompt",
                is_sidechain=True,
                agent_id="agent-2",
            ),
            _make_assistant_entry(
                "ag2-2",
                "s1",
                "2025-07-01T10:03:40.000Z",
                "ag2-1",
                "Agent2 reply",
                is_sidechain=True,
                agent_id="agent-2",
            ),
        ]

        _write_jsonl(
            tmp_path / "session.jsonl",
            main_entries + agent1_entries + agent2_entries,
        )

        result, tree = load_directory_transcripts(tmp_path, silent=True)
        uuids = [getattr(e, "uuid", None) for e in result]

        # Each agent should appear after its anchor and before the next main entry
        assert uuids.index("ag1-1") > uuids.index("u2")
        assert uuids.index("ag1-2") < uuids.index("a2")
        assert uuids.index("ag2-1") > uuids.index("u3")
        assert uuids.index("ag2-2") < uuids.index("a3")

        # Two synthetic agent sessions
        agent_sids = [sid for sid in tree.sessions if "#agent-" in sid]
        assert len(agent_sids) == 2

    def test_agent_in_branch(self, tmp_path: Path) -> None:
        """Agent anchored inside a within-session fork attaches to the branch."""
        from claude_code_log.renderer import generate_template_messages

        # Trunk: u1 → a1 (fork point)
        # Branch 1: b1_u (rewind from a1, different timestamp) → b1_a
        #   with agent anchored at b1_u
        # Branch 2: b2_u (rewind from a1, different timestamp)
        main_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("a1", "s1", "2025-07-01T10:01:00.000Z", "u1"),
            # Branch 1: user rewind from a1
            _make_user_entry(
                "b1_u",
                "s1",
                "2025-07-01T10:02:00.000Z",
                "a1",
                "Branch 1",
                agent_id="agent-b1",
            ),
            _make_assistant_entry(
                "b1_a",
                "s1",
                "2025-07-01T10:03:00.000Z",
                "b1_u",
            ),
            # Branch 2: user rewind from a1 (different timestamp = real fork)
            _make_user_entry(
                "b2_u",
                "s1",
                "2025-07-01T10:04:00.000Z",
                "a1",
                "Branch 2",
            ),
        ]
        agent_entries = [
            _make_user_entry(
                "ag1",
                "s1",
                "2025-07-01T10:02:30.000Z",
                None,
                "Agent in branch",
                is_sidechain=True,
                agent_id="agent-b1",
            ),
            _make_assistant_entry(
                "ag2",
                "s1",
                "2025-07-01T10:02:40.000Z",
                "ag1",
                "Agent reply",
                is_sidechain=True,
                agent_id="agent-b1",
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + agent_entries)

        result, tree = load_directory_transcripts(tmp_path, silent=True)

        # Agent session's parent should be the branch pseudo-session, not trunk
        agent_sids = [sid for sid in tree.sessions if "#agent-" in sid]
        assert len(agent_sids) == 1
        agent_dl = tree.sessions[agent_sids[0]]
        # The branch pseudo-session has format "s1@{child_uuid[:12]}"
        assert agent_dl.parent_session_id is not None
        assert "@" in agent_dl.parent_session_id, (
            f"Agent should be child of branch, got parent={agent_dl.parent_session_id}"
        )
        assert agent_dl.attachment_uuid == "b1_u"

        # End-to-end rendering: agent messages should appear in the branch,
        # not get regrouped under the trunk
        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        root_messages, session_nav, context = generate_template_messages(
            messages, session_tree=session_tree
        )

        # Verify message ordering: agent messages should be in the branch
        # block (after b1_u anchor, before branch 2's b2_u)
        msg_uuids = {m.meta.uuid: m.message_index for m in context.messages}
        assert "ag1" in msg_uuids
        assert "b1_u" in msg_uuids
        assert "b2_u" in msg_uuids
        assert msg_uuids["b1_u"] < msg_uuids["ag1"] < msg_uuids["b2_u"]  # type: ignore[operator]

    def test_agent_messages_coalesced_into_parent_session(self, tmp_path: Path) -> None:
        """Agent message counts and tokens fold into parent session aggregates.

        Regression test: agent messages must not be dropped from session
        metadata used for pagination and cache. They should be counted under
        the parent session.
        """
        # Main session: user → assistant(anchor, agentId) → user(next)
        main_entries = [
            _make_user_entry("u1", "s1", "2025-01-01T00:00:00Z", text="Hello"),
            _make_assistant_entry(
                "a1",
                "s1",
                "2025-01-01T00:00:01Z",
                parent_uuid="u1",
                agent_id="ag1",
            ),
            _make_user_entry(
                "u2",
                "s1",
                "2025-01-01T00:00:05Z",
                parent_uuid="a1",
                text="Continue",
            ),
        ]
        # Agent sidechain: 2 entries (user + assistant)
        agent_entries = [
            _make_user_entry(
                "ag_u1",
                "s1",
                "2025-01-01T00:00:02Z",
                is_sidechain=True,
                agent_id="ag1",
                text="agent task",
            ),
            _make_assistant_entry(
                "ag_a1",
                "s1",
                "2025-01-01T00:00:03Z",
                parent_uuid="ag_u1",
                is_sidechain=True,
                agent_id="ag1",
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", main_entries + agent_entries)

        messages, _tree = load_directory_transcripts(tmp_path, silent=True)

        # Build session data (used for pagination page assignment)
        session_data = _build_session_data_from_messages(messages)

        # Only the parent session should exist — no agent-synthetic session
        assert "s1" in session_data
        assert not any("#agent-" in sid for sid in session_data)

        s1 = session_data["s1"]
        # message_count should include both main (3) and agent (2) entries
        assert s1.message_count == 5

        # Token totals should include agent assistant entry (10 input, 5 output)
        # Main has 1 assistant (a1: 10 input, 5 output)
        # Agent has 1 assistant (ag_a1: 10 input, 5 output)
        assert s1.total_input_tokens == 20
        assert s1.total_output_tokens == 10


# =============================================================================
# Test: render_session_id does not leak across sessions
# =============================================================================


class TestRenderSessionResetAcrossSessions:
    """Pin that a within-session branch in session A doesn't leak its
    render_session_id into session B.

    Historical context: pre-D11 the renderer tracked the current
    render session via a loop-local ``current_render_session``
    variable, set on each branch-start trigger and cleared on each
    new trunk session. An early bug let the variable carry across
    sessions and pollute the next trunk's messages with a stale
    branch sid. D11 (``crux-derive-render-session-id-from-dag``)
    replaced the loop variable with a precomputed
    ``uuid_to_render_sid`` map derived from the SessionTree, so the
    polluted-state shape is structurally impossible — each uuid's
    render_session_id is determined by its DAG line, not by walk
    order. The test still pins the OUTCOME (s2 messages resolve to
    ``s2``), which is what any future implementation must continue
    to guarantee.
    """

    def test_second_session_not_polluted_by_first_session_branch(
        self, tmp_path: Path
    ) -> None:
        """Messages in session 2 should not inherit session 1's branch render_session_id."""
        from claude_code_log.renderer import generate_template_messages

        # Session s1 with a fork: u1 → a1, then both u2a and u2b branch from a1
        s1_entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_assistant_entry("a1", "s1", "2025-07-01T10:01:00.000Z", "u1"),
            # Fork: two children of a1
            _make_user_entry("u2a", "s1", "2025-07-01T10:02:00.000Z", "a1", "Branch A"),
            _make_assistant_entry("a2a", "s1", "2025-07-01T10:03:00.000Z", "u2a"),
            _make_user_entry("u2b", "s1", "2025-07-01T10:02:01.000Z", "a1", "Branch B"),
            _make_assistant_entry("a2b", "s1", "2025-07-01T10:03:01.000Z", "u2b"),
        ]

        # Session s2: separate session, should NOT inherit s1's branch state
        s2_entries = [
            _make_user_entry(
                "u3", "s2", "2025-07-01T11:00:00.000Z", None, "New session"
            ),
            _make_assistant_entry("a3", "s2", "2025-07-01T11:01:00.000Z", "u3"),
        ]

        _write_jsonl(tmp_path / "s1.jsonl", s1_entries)
        _write_jsonl(tmp_path / "s2.jsonl", s2_entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _, _, ctx = generate_template_messages(messages, session_tree=session_tree)

        # Find messages from session s2 by UUID
        s2_msgs = [m for m in ctx.messages if m.meta.uuid in ("u3", "a3")]
        assert len(s2_msgs) == 2, f"Expected 2 s2 messages, got {len(s2_msgs)}"

        for msg in s2_msgs:
            assert msg.render_session_id == "s2", (
                f"Message {msg.meta.uuid} has render_session_id={msg.render_session_id!r}, "
                f"expected 's2' — branch tracking from s1 leaked into s2"
            )

    def test_branch_first_uuid_filtered_out_still_groups_correctly(
        self, tmp_path: Path
    ) -> None:
        """The D11 latent-bug regression.

        Pre-D11 the renderer derived ``render_session_id`` from a
        loop-local ``current_render_session`` variable flipped at
        the branch-start trigger (the line whose uuid matched the
        branch's ``first_uuid``). If ``_filter_by_detail`` dropped
        that ``first_uuid`` entry at non-FULL detail, the trigger
        never fired and subsequent surviving branch entries silently
        inherited a stale render_session_id — landing in the wrong
        session group in the final tree.

        D11 replaces the loop variable with a precomputed
        ``uuid_to_render_sid`` map derived from the SessionTree.
        Every uuid in a branch DAG-line maps to that branch's sid
        regardless of which entries survive filtering, so the fix
        is structural.

        Fixture shape that triggers the bug:

        - Trunk: ``u`` (user) → ``a`` (assistant, ends in tool_use).
        - Fork at ``a``: two assistant children with ONLY a
          ``tool_use`` content item (no text). Both are dropped by
          ``_filter_by_detail`` at MINIMAL (strip_types = ThinkingContent,
          ToolUseContent, ToolResultContent → no text_items left).
        - Branch 1 continues: ``c1`` (user, parent ``b1``).
        - Branch 2 continues: ``c2`` (user, parent ``b2``).

        At MINIMAL, ``b1`` and ``b2`` are stripped from
        ``filtered_messages``, so the pre-D11 branch-start trigger
        never fires for either branch. ``c1`` and ``c2`` would
        then both fall through to the trunk's ``render_session_id``
        ("s1") instead of their respective branch sids — the bug.

        The map-driven derivation reads grouping from the
        SessionTree (built from PRE-filter messages), so
        ``uuid_to_render_sid[c1]`` is branch1's sid and
        ``uuid_to_render_sid[c2]`` is branch2's sid regardless of
        filtering.
        """
        from claude_code_log.models import DetailLevel
        from claude_code_log.renderer import generate_template_messages

        # Helper: assistant with ONLY a tool_use content item (no
        # text). At MINIMAL detail, _filter_by_detail strips
        # ToolUseContent and drops the entry entirely (no surviving
        # text_items).
        def _make_assistant_tool_only(
            uuid: str, parent_uuid: str, timestamp: str
        ) -> dict[str, Any]:
            return {
                "type": "assistant",
                "timestamp": timestamp,
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": uuid,
                "requestId": f"req_{uuid}",
                "message": {
                    "id": uuid,
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{uuid}",
                            "name": "Grep",
                            "input": {"pattern": "x"},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }

        entries = [
            _make_user_entry("u", "s1", "2025-07-01T10:00:00.000Z", None, "hello"),
            _make_assistant_entry(
                "a", "s1", "2025-07-01T10:01:00.000Z", "u", "going to fork"
            ),
            # Branch 1: tool_use-only first entry (will be filtered out
            # at MINIMAL) → text-bearing user message (survives).
            _make_assistant_tool_only("b1", "a", "2025-07-01T10:02:00.000Z"),
            _make_user_entry(
                "c1", "s1", "2025-07-01T10:03:00.000Z", "b1", "BRANCH 1 LOCAL"
            ),
            # Branch 2 sibling — same shape (the fork-collapse logic in
            # dag.py treats this as a real fork because both children
            # have non-dead descendants).
            _make_assistant_tool_only("b2", "a", "2025-07-01T10:04:00.000Z"),
            _make_user_entry(
                "c2", "s1", "2025-07-01T10:05:00.000Z", "b2", "BRANCH 2 LOCAL"
            ),
        ]
        _write_jsonl(tmp_path / "s1.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        # Sanity: the SessionTree (built pre-filter) MUST see both
        # branches — otherwise the fixture doesn't actually exercise
        # the bug.
        assert session_tree is not None
        branch_sids = [sid for sid in session_tree.sessions if "@" in sid]
        assert len(branch_sids) == 2, (
            f"fixture must produce 2 branches; got {branch_sids}. "
            "If this fails, the fork-collapse heuristic in dag.py "
            "absorbed one of the siblings — adjust the fixture so "
            "both children have non-dead descendants of distinct types."
        )

        # Render at MINIMAL — b1 and b2 are stripped.
        _, _, ctx = generate_template_messages(
            messages,
            session_tree=session_tree,
            detail=DetailLevel.MINIMAL,
        )

        # b1 and b2 should NOT appear in the rendered messages
        # (sanity: confirms the filter is doing what we expect).
        rendered_uuids = {
            m.meta.uuid for m in ctx.messages if m is not None and m.meta.uuid
        }
        assert "b1" not in rendered_uuids, (
            f"b1 should be filtered out at MINIMAL; got rendered_uuids={rendered_uuids}"
        )
        assert "b2" not in rendered_uuids

        # c1 and c2 SHOULD appear and resolve to their respective
        # branch sids — NOT to "s1" (trunk).
        by_uuid = {
            m.meta.uuid: m
            for m in ctx.messages
            if m is not None and m.meta.uuid in ("c1", "c2")
        }
        assert set(by_uuid) == {"c1", "c2"}, (
            f"expected c1+c2 in rendered messages; got {set(by_uuid)}"
        )

        # Each branch's surviving user message should have render_session_id
        # equal to one of the branch sids (and NOT "s1").
        c1_sid = by_uuid["c1"].render_session_id
        c2_sid = by_uuid["c2"].render_session_id
        assert c1_sid != "s1", (
            "c1.render_session_id == 's1' — the loop-variable bug. "
            "Expected a branch sid ('s1@<uuid12>')."
        )
        assert c2_sid != "s1"
        assert "@" in c1_sid, (
            f"c1.render_session_id must be a branch sid; got {c1_sid!r}"
        )
        assert "@" in c2_sid
        # The two branches must be DIFFERENT (each user message
        # belongs to its own branch, not to both).
        assert c1_sid != c2_sid, (
            f"c1 and c2 must resolve to DIFFERENT branch sids; got both = {c1_sid!r}"
        )
        # And each must be one of the SessionTree-known branch sids.
        assert c1_sid in branch_sids
        assert c2_sid in branch_sids

    def test_trunk_header_registers_before_branch_when_trunk_msgs_filtered(
        self, tmp_path: Path
    ) -> None:
        """CodeRabbit-flagged regression on the D11 map-driven trigger.

        Scenario: every trunk-session entry is filtered out at non-
        FULL detail (e.g., all the trunk's assistants are tool_use-
        only and stripped at MINIMAL), so the FIRST surviving
        message processed for that trunk session is a BRANCH
        descendant. Without the trunk-header-before-branch-header
        guard, the branch trigger registers the branch header before
        any trunk message has registered the trunk header. Result:

        - ``ctx.messages`` order: branch_header(0), trunk_header(1),
          branch's content(2)...
        - ``_reorder_session_template_messages`` preserves the
          session-header encounter order → branch ends up as a root
          section, NOT nested under its parent trunk session.
        - The branch header's ``parent_message_index`` is also
          ``None`` because the trunk header isn't in
          ``ctx.session_first_message`` yet when the branch header
          is built.

        The fix: when about to register a branch header, FIRST
        ensure the branch's parent trunk session header is in
        ``seen_sessions`` and ``ctx.session_first_message``; create
        it on the fly if not. Pre-D11 the bug couldn't manifest
        because the branch trigger fired only on the branch's
        ``first_uuid``, and that uuid's own meta.session_id is the
        trunk sid which would create the trunk header on the same
        message iteration before any branch content; D11's wider
        trigger (any surviving branch line uuid) exposed the gap.

        Fixture: trunk's only message ``a1`` is a tool_use-only
        assistant (filtered at MINIMAL). Two branches off it, each
        also starting with a tool_use-only assistant (filtered)
        then a surviving user-text. At MINIMAL only ``c1`` and
        ``c2`` survive — both are branch descendants whose parent
        trunk has no surviving entry.

        Asserts (failure modes the fix prevents):

        - Trunk header for ``s1`` exists in ``ctx.messages`` and
          appears BEFORE any branch header for ``s1@...``.
        - Every branch header's ``parent_message_index`` points to
          a registered message (NOT ``None``).
        """
        from claude_code_log.models import DetailLevel, SessionHeaderMessage
        from claude_code_log.renderer import generate_template_messages

        def _make_assistant_tool_only(
            uuid: str, parent_uuid: str | None, timestamp: str
        ) -> dict[str, Any]:
            return {
                "type": "assistant",
                "timestamp": timestamp,
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": uuid,
                "requestId": f"req_{uuid}",
                "message": {
                    "id": uuid,
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{uuid}",
                            "name": "Grep",
                            "input": {"pattern": "x"},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }

        # Trunk root is a tool-only assistant → filtered at MINIMAL.
        # Fork at a1 → two tool-only assistant siblings (both
        # filtered) each followed by a surviving user-text.
        entries = [
            _make_assistant_tool_only("a1", None, "2025-09-01T10:01:00.000Z"),
            _make_assistant_tool_only("b1", "a1", "2025-09-01T10:02:00.000Z"),
            _make_user_entry(
                "c1", "s1", "2025-09-01T10:03:00.000Z", "b1", "BRANCH 1 LOCAL"
            ),
            _make_assistant_tool_only("b2", "a1", "2025-09-01T10:04:00.000Z"),
            _make_user_entry(
                "c2", "s1", "2025-09-01T10:05:00.000Z", "b2", "BRANCH 2 LOCAL"
            ),
        ]
        _write_jsonl(tmp_path / "s1.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        assert session_tree is not None
        branch_sids = sorted(sid for sid in session_tree.sessions if "@" in sid)
        assert len(branch_sids) == 2, (
            f"fixture must produce 2 branches; got {branch_sids}"
        )

        _, _, ctx = generate_template_messages(
            messages,
            session_tree=session_tree,
            detail=DetailLevel.MINIMAL,
        )

        # Collect every session header in encounter order.
        header_order: list[tuple[int, str, bool, object]] = []
        for i, m in enumerate(ctx.messages):
            if m is not None and isinstance(m.content, SessionHeaderMessage):
                header_order.append(
                    (
                        i,
                        m.content.session_id,
                        m.content.is_branch,
                        m.content.parent_message_index,
                    )
                )

        # 1) Trunk header for "s1" MUST be registered (and it must
        #    NOT be a branch header).
        trunk_headers = [h for h in header_order if h[1] == "s1" and not h[2]]
        assert len(trunk_headers) == 1, (
            "expected exactly 1 trunk header for 's1' in ctx.messages; "
            f"got {trunk_headers}. Full header order: {header_order}"
        )
        trunk_idx = trunk_headers[0][0]

        # 2) Every branch header for an "s1@..." sid MUST appear
        #    AFTER the trunk header.
        branch_headers = [h for h in header_order if h[2]]
        assert len(branch_headers) == 2, (
            f"expected 2 branch headers; got {branch_headers}"
        )
        for idx, sid, _is_branch, parent_msg_idx in branch_headers:
            assert idx > trunk_idx, (
                f"branch header {sid!r} at index {idx} appears BEFORE "
                f"trunk header at index {trunk_idx} — _reorder_session_"
                f"template_messages will emit it as a root, not nested "
                "under the trunk. Header order: " + str(header_order)
            )
            # 3) The fork anchor a1 is a tool-only assistant, filtered at
            #    MINIMAL. Since the #233 follow-up, a filtered fork anchor is
            #    KEPT as a fork_only landmark (not ghosted to None), so the
            #    fork point stays visible and the branch's back-link resolves
            #    to it — active, not omitted. (Pre-#233-follow-up this asserted
            #    parent_message_index is None.) The D11 invariant under test —
            #    trunk header registered BEFORE the branch (assertions 1+2) —
            #    is unaffected either way.
            assert parent_msg_idx is not None, (
                f"branch header {sid!r}.parent_message_index={parent_msg_idx}: "
                "expected a live index — its fork anchor 'a1' is kept as a "
                "fork_only landmark at MINIMAL, so the backlink resolves to it. "
                "Header order: " + str(header_order)
            )
            anchor = ctx.get(parent_msg_idx)  # type: ignore[arg-type]
            assert (
                anchor is not None
                and anchor.meta.uuid == "a1"
                and anchor.fork_only is True
            ), (
                f"branch header {sid!r} back-link must resolve to the live "
                f"fork_only landmark 'a1'; got {anchor!r}."
            )


# =============================================================================
# Test: PassthroughTranscriptEntry for DAG chain continuity
# =============================================================================


def _make_passthrough_entry(
    uuid: str,
    session_id: str,
    timestamp: str,
    parent_uuid: str | None = None,
    entry_type: str = "attachment",
) -> dict[str, Any]:
    """Helper to create a passthrough (non-rendered) entry dict."""
    return {
        "type": entry_type,
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        # No "message" field — this is not a user/assistant entry
    }


class TestPassthroughDagChain:
    """Test that passthrough entries (attachment, etc.) preserve DAG chain."""

    def test_attachment_preserves_parent_chain(self, tmp_path: Path) -> None:
        """Assistant whose parentUuid points to an attachment should not be a false root."""
        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_passthrough_entry(
                "att1", "s1", "2025-07-01T10:00:30.000Z", "u1", "attachment"
            ),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T10:01:00.000Z", "att1", "Reply"
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, tree = load_directory_transcripts(tmp_path, silent=True)

        # All three entries should be in the DAG
        assert "u1" in tree.nodes
        assert "att1" in tree.nodes
        assert "a1" in tree.nodes

        # a1's parent should be att1, att1's parent should be u1
        assert tree.nodes["a1"].parent_uuid == "att1"
        assert tree.nodes["att1"].parent_uuid == "u1"

        # a1 should NOT be a false root — the session should have only 1 root (u1)
        s1 = tree.sessions.get("s1")
        assert s1 is not None
        assert s1.uuids[0] == "u1", "u1 should be the first entry in the session"

    def test_attachment_not_rendered(self, tmp_path: Path) -> None:
        """Passthrough entries should not appear in rendered output."""
        from claude_code_log.renderer import generate_template_messages

        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Hello"),
            _make_passthrough_entry(
                "att1", "s1", "2025-07-01T10:00:30.000Z", "u1", "attachment"
            ),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T10:01:00.000Z", "att1", "World"
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _, _, ctx = generate_template_messages(messages, session_tree=session_tree)

        # No message should have uuid "att1"
        rendered_uuids = [m.meta.uuid for m in ctx.messages if m.meta.uuid]
        assert "att1" not in rendered_uuids
        # But user and assistant should be rendered
        assert "u1" in rendered_uuids
        assert "a1" in rendered_uuids

    def test_multiple_passthrough_types(self, tmp_path: Path) -> None:
        """Various unknown types with uuid should all become passthrough entries."""
        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_passthrough_entry(
                "p1", "s1", "2025-07-01T10:00:10.000Z", "u1", "attachment"
            ),
            _make_passthrough_entry(
                "p2", "s1", "2025-07-01T10:00:20.000Z", "p1", "other-unknown-type"
            ),
            _make_passthrough_entry(
                "p3", "s1", "2025-07-01T10:00:30.000Z", "p2", "unknown-future-type"
            ),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T10:01:00.000Z", "p3", "Reply"
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, tree = load_directory_transcripts(tmp_path, silent=True)

        # All should be in DAG
        for uid in ["u1", "p1", "p2", "p3", "a1"]:
            assert uid in tree.nodes, f"{uid} should be in DAG"

        # Chain should be intact: u1 → p1 → p2 → p3 → a1
        assert tree.nodes["a1"].parent_uuid == "p3"
        assert tree.nodes["p3"].parent_uuid == "p2"
        assert tree.nodes["p2"].parent_uuid == "p1"
        assert tree.nodes["p1"].parent_uuid == "u1"

    def test_passthrough_excluded_from_session_data(self, tmp_path: Path) -> None:
        """Passthrough entries should not inflate session message counts."""
        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Start"),
            _make_passthrough_entry(
                "att1", "s1", "2025-07-01T10:00:30.000Z", "u1", "attachment"
            ),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T10:01:00.000Z", "att1", "Reply"
            ),
        ]

        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, _ = load_directory_transcripts(tmp_path, silent=True)
        session_data = _build_session_data_from_messages(messages)

        # Only user + assistant should be counted (not the attachment)
        assert session_data["s1"].message_count == 2


# =============================================================================
# Test: compact_boundary nav landmarks
# =============================================================================


COMPACTED_SUMMARY_BODY = (
    "This session is being continued from a previous conversation that "
    "ran out of context. The summary below covers the earlier portion."
)


def _make_compact_boundary(
    uuid: str,
    session_id: str,
    timestamp: str,
    pre_tokens: int = 100_000,
) -> dict[str, Any]:
    """Synthesize a system/compact_boundary entry (always parent-null)."""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": timestamp,
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": "manual", "preTokens": pre_tokens},
    }


def _make_compacted_summary(
    uuid: str,
    session_id: str,
    timestamp: str,
    parent_uuid: str,
) -> dict[str, Any]:
    """Synthesize the user entry that carries the compacted summary text."""
    return {
        "type": "user",
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "isCompactSummary": True,
        "message": {"role": "user", "content": COMPACTED_SUMMARY_BODY},
    }


class TestCompactBoundaryNav:
    """Compacted summaries should appear as navigational landmarks."""

    def test_single_boundary_produces_nav_entry(self, tmp_path: Path) -> None:
        """One compaction → one compaction-point nav item under the session."""
        from claude_code_log.renderer import generate_template_messages

        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Initial"),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T10:01:00.000Z", "u1", "Reply"
            ),
            _make_compact_boundary("cb1", "s1", "2025-07-01T11:00:00.000Z"),
            _make_compacted_summary("sum1", "s1", "2025-07-01T11:00:01.000Z", "cb1"),
            _make_assistant_entry(
                "a2", "s1", "2025-07-01T11:00:02.000Z", "sum1", "Continuing"
            ),
        ]
        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _root_msgs, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        comp_items = [n for n in session_nav if n.get("is_compaction_point")]
        assert len(comp_items) == 1, (
            f"Expected one compaction-point nav item; got: {session_nav}"
        )
        assert comp_items[0]["parent_session_id"] == "s1"
        assert comp_items[0]["message_index"] is not None
        # Label carries preTokens (100k default from _make_compact_boundary)
        # and a formatted timestamp.
        label = comp_items[0]["first_user_message"]
        assert "100k tokens" in label, f"Expected '100k tokens' in {label!r}"
        assert "2025" in label, f"Expected timestamp in {label!r}"

    def test_multiple_boundaries_in_timestamp_order(self, tmp_path: Path) -> None:
        """Two compactions → two nav items, ordered chronologically."""
        from claude_code_log.renderer import generate_template_messages

        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Initial"),
            _make_compact_boundary("cb1", "s1", "2025-07-01T11:00:00.000Z"),
            _make_compacted_summary("sum1", "s1", "2025-07-01T11:00:01.000Z", "cb1"),
            _make_assistant_entry(
                "a1", "s1", "2025-07-01T11:00:02.000Z", "sum1", "After first"
            ),
            _make_compact_boundary("cb2", "s1", "2025-07-01T12:00:00.000Z"),
            _make_compacted_summary("sum2", "s1", "2025-07-01T12:00:01.000Z", "cb2"),
            _make_assistant_entry(
                "a2", "s1", "2025-07-01T12:00:02.000Z", "sum2", "After second"
            ),
        ]
        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _root_msgs, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        comp_items = [n for n in session_nav if n.get("is_compaction_point")]
        assert len(comp_items) == 2
        # Ordered by first_timestamp
        assert comp_items[0]["first_timestamp"] < comp_items[1]["first_timestamp"]

    def test_no_boundary_produces_no_nav_entry(self, tmp_path: Path) -> None:
        """A plain session emits no compaction-point items."""
        from claude_code_log.renderer import generate_template_messages

        entries = [
            _make_user_entry("u1", "s1", "2025-07-01T10:00:00.000Z", None, "Q"),
            _make_assistant_entry("a1", "s1", "2025-07-01T10:01:00.000Z", "u1", "A"),
        ]
        _write_jsonl(tmp_path / "session.jsonl", entries)

        messages, session_tree = load_directory_transcripts(tmp_path, silent=True)
        _root_msgs, session_nav, _ctx = generate_template_messages(
            messages, session_tree=session_tree
        )

        assert not any(n.get("is_compaction_point") for n in session_nav)

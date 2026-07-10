"""Tests for the DAG-based message ordering module."""

import json
import logging
from pathlib import Path
from typing import Callable, Optional, TypeVar

import pytest

from claude_code_log.dag import (
    MessageNode,
    SessionTree,
    build_dag,
    build_dag_from_entries,
    build_message_index,
    extract_session_dag_lines,
    traverse_session_tree,
)
from claude_code_log.factories import create_transcript_entry
from claude_code_log.models import (
    SummaryTranscriptEntry,
    TranscriptEntry,
)

T = TypeVar("T")

TEST_DATA = Path(__file__).parent / "test_data"
REAL_PROJECTS = TEST_DATA / "real_projects"


def load_entries_from_jsonl(path: Path) -> list[TranscriptEntry]:
    """Load transcript entries from a JSONL file, skipping unparseable lines."""
    entries: list[TranscriptEntry] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            entry_type = data.get("type")
            if entry_type in (
                "user",
                "assistant",
                "summary",
                "system",
                "queue-operation",
            ):
                entries.append(create_transcript_entry(data))
    return entries


def load_project_entries(project_dir: Path) -> list[TranscriptEntry]:
    """Load all entries from a project directory (excluding agent files)."""
    entries: list[TranscriptEntry] = []
    for jsonl_file in sorted(project_dir.glob("*.jsonl")):
        if jsonl_file.name.startswith("agent-"):
            continue
        entries.extend(load_entries_from_jsonl(jsonl_file))
    return entries


# =============================================================================
# Test: Single session (dag_simple.jsonl)
# =============================================================================


class TestSingleSession:
    """Tests using dag_simple.jsonl: a→b→c→d→e in session s1."""

    @pytest.fixture()
    def tree(self) -> SessionTree:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        return build_dag_from_entries(entries)

    def test_single_session_one_dagline(self, tree: SessionTree) -> None:
        assert len(tree.sessions) == 1
        assert "s1" in tree.sessions

    def test_single_session_chain_order(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s1"]
        assert dag_line.uuids == ["a", "b", "c", "d", "e"]

    def test_single_session_is_root(self, tree: SessionTree) -> None:
        assert tree.roots == ["s1"]

    def test_single_session_no_junction_points(self, tree: SessionTree) -> None:
        assert tree.junction_points == {}

    def test_single_session_traversal(self, tree: SessionTree) -> None:
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "e"]

    def test_single_session_first_timestamp(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s1"]
        assert dag_line.first_timestamp == "2025-07-01T10:00:00.000Z"

    def test_single_session_no_parent(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s1"]
        assert dag_line.parent_session_id is None
        assert dag_line.attachment_uuid is None


# =============================================================================
# Test: Resume session (dag_resume.jsonl)
# =============================================================================


class TestResumeSession:
    """Tests using dag_resume.jsonl: s1(a→b→c→d→e), s2(f→g→h) where f.parent=e."""

    @pytest.fixture()
    def tree(self) -> SessionTree:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_resume.jsonl")
        return build_dag_from_entries(entries)

    def test_two_sessions(self, tree: SessionTree) -> None:
        assert len(tree.sessions) == 2
        assert "s1" in tree.sessions
        assert "s2" in tree.sessions

    def test_s1_chain(self, tree: SessionTree) -> None:
        assert tree.sessions["s1"].uuids == ["a", "b", "c", "d", "e"]

    def test_s2_chain(self, tree: SessionTree) -> None:
        assert tree.sessions["s2"].uuids == ["f", "g", "h"]

    def test_s1_is_root(self, tree: SessionTree) -> None:
        assert tree.roots == ["s1"]

    def test_s2_parent_is_s1(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s2"]
        assert dag_line.parent_session_id == "s1"
        assert dag_line.attachment_uuid == "e"

    def test_junction_at_e(self, tree: SessionTree) -> None:
        assert "e" in tree.junction_points
        jp = tree.junction_points["e"]
        assert jp.session_id == "s1"
        assert jp.target_sessions == ["s2"]

    def test_traversal_order(self, tree: SessionTree) -> None:
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "e", "f", "g", "h"]


# =============================================================================
# Test: Fork session (dag_fork.jsonl)
# =============================================================================


class TestForkSession:
    """Tests using dag_fork.jsonl: s1(a→e), s2(f→h from e), s3(i→k from c)."""

    @pytest.fixture()
    def tree(self) -> SessionTree:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_fork.jsonl")
        return build_dag_from_entries(entries)

    def test_three_sessions(self, tree: SessionTree) -> None:
        assert len(tree.sessions) == 3

    def test_s1_chain(self, tree: SessionTree) -> None:
        assert tree.sessions["s1"].uuids == ["a", "b", "c", "d", "e"]

    def test_s2_chain(self, tree: SessionTree) -> None:
        assert tree.sessions["s2"].uuids == ["f", "g", "h"]

    def test_s3_chain(self, tree: SessionTree) -> None:
        assert tree.sessions["s3"].uuids == ["i", "j", "k"]

    def test_only_s1_is_root(self, tree: SessionTree) -> None:
        assert tree.roots == ["s1"]

    def test_s2_attaches_at_e(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s2"]
        assert dag_line.parent_session_id == "s1"
        assert dag_line.attachment_uuid == "e"

    def test_s3_attaches_at_c(self, tree: SessionTree) -> None:
        dag_line = tree.sessions["s3"]
        assert dag_line.parent_session_id == "s1"
        assert dag_line.attachment_uuid == "c"

    def test_two_junction_points(self, tree: SessionTree) -> None:
        assert len(tree.junction_points) == 2
        assert "c" in tree.junction_points
        assert "e" in tree.junction_points

    def test_junction_c_targets_s3(self, tree: SessionTree) -> None:
        jp = tree.junction_points["c"]
        assert jp.session_id == "s1"
        assert jp.target_sessions == ["s3"]

    def test_junction_e_targets_s2(self, tree: SessionTree) -> None:
        jp = tree.junction_points["e"]
        assert jp.session_id == "s1"
        assert jp.target_sessions == ["s2"]

    def test_traversal_depth_first(self, tree: SessionTree) -> None:
        """Depth-first: s1 entries, then at junction c visit s3 (fork),
        continue s1, then at junction e visit s2 (continue)."""
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        # s1: a,b,c → s3: i,j,k → s1: d,e → s2: f,g,h
        assert uuids == ["a", "b", "c", "i", "j", "k", "d", "e", "f", "g", "h"]


# =============================================================================
# Test: Deduplication
# =============================================================================


class TestDeduplication:
    """Test that duplicate uuids are resolved by keeping earliest session."""

    def test_dedup_keeps_earliest_session(self) -> None:
        """Same uuid in two sessions; entry from earlier session wins."""
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        # Manually add a duplicate of uuid "c" with a later session
        dup_data = {
            "type": "user",
            "timestamp": "2025-07-02T10:00:00.000Z",
            "parentUuid": "b",
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s_later",
            "version": "1.0.0",
            "uuid": "c",
            "message": {"role": "user", "content": [{"type": "text", "text": "Dup"}]},
        }
        entries.append(create_transcript_entry(dup_data))

        nodes = build_message_index(entries)
        # "c" should belong to s1 (timestamp 2025-07-01) not s_later (2025-07-02)
        assert nodes["c"].session_id == "s1"

    def test_dedup_replaces_with_earlier(self) -> None:
        """If later-loaded entry is from an earlier session, it replaces."""
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        # Add a duplicate of uuid "c" from an EARLIER session
        dup_data = {
            "type": "user",
            "timestamp": "2025-06-30T10:00:00.000Z",
            "parentUuid": "b",
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s_earlier",
            "version": "1.0.0",
            "uuid": "c",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Earlier"}],
            },
        }
        entries.append(create_transcript_entry(dup_data))

        nodes = build_message_index(entries)
        # "c" should now belong to s_earlier
        assert nodes["c"].session_id == "s_earlier"


# =============================================================================
# Test: Junction Points
# =============================================================================


class TestJunctionPoints:
    """Detailed junction point tests."""

    def test_no_junctions_single_session(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        tree = build_dag_from_entries(entries)
        assert len(tree.junction_points) == 0

    def test_single_junction_resume(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_resume.jsonl")
        tree = build_dag_from_entries(entries)
        assert len(tree.junction_points) == 1
        assert "e" in tree.junction_points

    def test_multiple_junctions_fork(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_fork.jsonl")
        tree = build_dag_from_entries(entries)
        assert len(tree.junction_points) == 2
        # Both c and e are junctions
        assert set(tree.junction_points.keys()) == {"c", "e"}

    def test_junction_target_sessions_ordered_chronologically(self) -> None:
        """If multiple sessions fork from the same point, targets are ordered."""
        # Create data where two sessions both fork from the same message
        base = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        fork1_data = {
            "type": "user",
            "timestamp": "2025-07-02T10:00:00.000Z",
            "parentUuid": "c",
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s_fork1",
            "version": "1.0.0",
            "uuid": "f1",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Fork 1"}],
            },
        }
        fork2_data = {
            "type": "user",
            "timestamp": "2025-07-01T12:00:00.000Z",
            "parentUuid": "c",
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s_fork2",
            "version": "1.0.0",
            "uuid": "f2",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Fork 2"}],
            },
        }
        base.append(create_transcript_entry(fork1_data))
        base.append(create_transcript_entry(fork2_data))

        tree = build_dag_from_entries(base)
        jp = tree.junction_points["c"]
        # s_fork2 is earlier (2025-07-01T12:00) than s_fork1 (2025-07-02T10:00)
        assert jp.target_sessions == ["s_fork2", "s_fork1"]


# =============================================================================
# Test: Traversal Order
# =============================================================================


class TestTraversalOrder:
    """Test depth-first session tree traversal produces correct order."""

    def test_simple_traversal(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        assert len(result) == 5
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "e"]

    def test_resume_traversal(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_resume.jsonl")
        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "e", "f", "g", "h"]

    def test_fork_traversal(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_fork.jsonl")
        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        # Depth-first: at junction c (after emitting c), visit s3 first
        # then continue s1, at junction e visit s2
        assert uuids == ["a", "b", "c", "i", "j", "k", "d", "e", "f", "g", "h"]

    def test_traversal_returns_entries(self) -> None:
        """Verify traversal returns actual TranscriptEntry objects."""
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        for entry in result:
            assert hasattr(entry, "type")
            assert hasattr(entry, "uuid")


# =============================================================================
# Test: Real project data
# =============================================================================


EXPERIMENTS_DIR = REAL_PROJECTS / "-src-experiments-claude_p"


@pytest.mark.skipif(
    not EXPERIMENTS_DIR.exists(),
    reason="Real project test data not available",
)
class TestRealProjectExperiments:
    """Test DAG construction against real project data.

    The -src-experiments-claude_p project has 4 independent sessions
    with no cross-session references, so should produce 4 root sessions.
    """

    @pytest.fixture()
    def tree(self) -> SessionTree:
        entries = load_project_entries(EXPERIMENTS_DIR)
        return build_dag_from_entries(entries)

    def test_loads_multiple_sessions(self, tree: SessionTree) -> None:
        assert len(tree.sessions) >= 4

    def test_all_sessions_are_roots(self, tree: SessionTree) -> None:
        """Independent sessions should all be roots."""
        # All sessions with DAG-lines should be root (no cross-refs)
        for session_id in tree.sessions:
            dag_line = tree.sessions[session_id]
            assert dag_line.parent_session_id is None, (
                f"Session {session_id} unexpectedly has parent "
                f"{dag_line.parent_session_id}"
            )

    def test_no_junction_points(self, tree: SessionTree) -> None:
        """Independent sessions should have no junction points."""
        assert len(tree.junction_points) == 0

    def test_each_session_has_entries(self, tree: SessionTree) -> None:
        """Each session's DAG-line should have at least one message."""
        for session_id, dag_line in tree.sessions.items():
            assert len(dag_line.uuids) > 0, f"Session {session_id} has empty DAG-line"

    def test_traversal_covers_all_entries(self, tree: SessionTree) -> None:
        """Traversal should include all entries from all sessions."""
        total_in_daglines = sum(len(dl.uuids) for dl in tree.sessions.values())
        result = traverse_session_tree(tree)
        assert len(result) == total_in_daglines

    def test_sessions_ordered_chronologically(self, tree: SessionTree) -> None:
        """Root sessions should be ordered by first_timestamp."""
        timestamps = [tree.sessions[sid].first_timestamp for sid in tree.roots]
        assert timestamps == sorted(timestamps)


# =============================================================================
# Test: Edge cases
# =============================================================================


class TestOrphanParent:
    """Test handling of parentUuid pointing to unknown uuid."""

    def test_orphan_treated_as_root(self) -> None:
        """A session whose first message has parentUuid pointing to
        an unknown uuid should be treated as a root session."""
        orphan_data = {
            "type": "user",
            "timestamp": "2025-07-01T10:00:00.000Z",
            "parentUuid": "nonexistent_uuid",
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s_orphan",
            "version": "1.0.0",
            "uuid": "orphan_a",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Orphan"}],
            },
        }
        entries: list[TranscriptEntry] = [create_transcript_entry(orphan_data)]
        tree = build_dag_from_entries(entries)

        assert "s_orphan" in tree.sessions
        assert tree.roots == ["s_orphan"]
        dag_line = tree.sessions["s_orphan"]
        assert dag_line.parent_session_id is None

    def test_orphan_with_children(self) -> None:
        """Orphan node still chains correctly within its session."""
        data = [
            {
                "type": "user",
                "timestamp": "2025-07-01T10:00:00.000Z",
                "parentUuid": "nonexistent",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "x",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Start"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-07-01T10:01:00.000Z",
                "parentUuid": "x",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "y",
                "requestId": "req_1",
                "message": {
                    "id": "y",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [{"type": "text", "text": "Reply"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        assert tree.sessions["s1"].uuids == ["x", "y"]
        assert tree.roots == ["s1"]


class TestSummaryEntriesSkipped:
    """Test that SummaryTranscriptEntry entries are excluded from DAG."""

    def test_summary_not_in_nodes(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        # Add a summary entry
        summary_data = {
            "type": "summary",
            "summary": "A test summary",
            "leafUuid": "e",
            "timestamp": "2025-07-01T10:05:00.000Z",
        }
        entries.append(create_transcript_entry(summary_data))

        nodes = build_message_index(entries)
        # Summary has no uuid, so it can't be in nodes
        for node in nodes.values():
            assert not isinstance(node.entry, SummaryTranscriptEntry)

    def test_summary_not_in_traversal(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        summary_data = {
            "type": "summary",
            "summary": "A test summary",
            "leafUuid": "e",
            "timestamp": "2025-07-01T10:05:00.000Z",
        }
        entries.append(create_transcript_entry(summary_data))

        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        # Should still be 5 entries (a-e), summary excluded
        assert len(result) == 5
        for entry in result:
            assert not isinstance(entry, SummaryTranscriptEntry)


class TestQueueOperationSkipped:
    """Test that QueueOperationTranscriptEntry entries are excluded from DAG."""

    def test_queue_op_not_in_nodes(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        queue_data = {
            "type": "queue-operation",
            "operation": "dequeue",
            "timestamp": "2025-07-01T09:59:00.000Z",
            "sessionId": "s1",
        }
        entries.append(create_transcript_entry(queue_data))

        nodes = build_message_index(entries)
        # queue-operation has no uuid field
        assert len(nodes) == 5  # Only a,b,c,d,e


# =============================================================================
# Test: Individual algorithm steps
# =============================================================================


class TestBuildMessageIndex:
    """Test build_message_index in isolation."""

    def test_indexes_all_entries(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        nodes = build_message_index(entries)
        assert set(nodes.keys()) == {"a", "b", "c", "d", "e"}

    def test_preserves_entry_data(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        nodes = build_message_index(entries)
        assert nodes["a"].session_id == "s1"
        assert nodes["a"].parent_uuid is None
        assert nodes["b"].parent_uuid == "a"

    def test_empty_entries(self) -> None:
        nodes = build_message_index([])
        assert nodes == {}


class TestBuildDAG:
    """Test build_dag (parent→children links)."""

    def test_children_populated(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        nodes = build_message_index(entries)
        build_dag(nodes)
        assert nodes["a"].children_uuids == ["b"]
        assert nodes["b"].children_uuids == ["c"]
        assert nodes["e"].children_uuids == []

    def test_root_has_no_parent(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        nodes = build_message_index(entries)
        build_dag(nodes)
        assert nodes["a"].parent_uuid is None

    def test_cross_session_children(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_resume.jsonl")
        nodes = build_message_index(entries)
        build_dag(nodes)
        # "e" has child "f" which is in a different session
        assert "f" in nodes["e"].children_uuids


class TestExtractSessionDAGLines:
    """Test extract_session_dag_lines."""

    def test_single_session_chain(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_simple.jsonl")
        nodes = build_message_index(entries)
        build_dag(nodes)
        sessions = extract_session_dag_lines(nodes)
        assert sessions["s1"].uuids == ["a", "b", "c", "d", "e"]

    def test_multi_session_chains(self) -> None:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_resume.jsonl")
        nodes = build_message_index(entries)
        build_dag(nodes)
        sessions = extract_session_dag_lines(nodes)
        assert sessions["s1"].uuids == ["a", "b", "c", "d", "e"]
        assert sessions["s2"].uuids == ["f", "g", "h"]


# =============================================================================
# Test: Cyclic parentUuid chains (regression for memory-exhaustion hang)
# =============================================================================


class TestCyclicParentChain:
    """Regression: cyclic parentUuid must not produce cyclic children_uuids.

    Real-world JSONL files occasionally contain cyclic parentUuid chains
    (e.g. A claims B as parent, B claims A as parent, or A claims itself).
    Earlier versions of build_dag detected the cycle in the parent chain
    and nulled one node's parent_uuid, but children_uuids had already been
    populated from the cyclic edges. Downstream walks (e.g.
    _walk_session_with_forks) followed children_uuids without a visited
    guard and looped forever, accumulating gigabytes of state.
    """

    @staticmethod
    def _make_entry(
        uuid: str, parent_uuid: str | None, sid: str = "s1", i: int = 0
    ) -> TranscriptEntry:
        data: dict[str, object] = {
            "type": "user",
            "timestamp": f"2025-07-01T10:00:{i:02d}.000Z",
            "parentUuid": parent_uuid,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": sid,
            "version": "1.0.0",
            "uuid": uuid,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"msg {uuid}"}],
            },
        }
        return create_transcript_entry(data)

    @staticmethod
    def _assert_no_child_cycle(nodes: dict[str, MessageNode]) -> None:
        """Walk children_uuids from every node; fail if any node revisits itself."""
        for start in nodes.values():
            visited: set[str] = set()
            stack: list[str] = [start.uuid]
            while stack:
                u = stack.pop()
                if u in visited:
                    pytest.fail(
                        f"Cycle in children_uuids reached {u} starting from {start.uuid}"
                    )
                visited.add(u)
                stack.extend(nodes[u].children_uuids)

    @staticmethod
    def _run_with_timeout(fn: Callable[[], T], seconds: float = 5.0) -> Optional[T]:
        """Run fn() in a thread; fail the test if it does not terminate in time.

        Without a timeout, a regression of this bug would hang the entire
        suite (and exhaust memory). The thread is left to die with the
        process — acceptable for a regression guard.
        """
        import threading

        result: list[T] = []
        error: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(fn())
            except BaseException as e:  # noqa: BLE001
                error.append(e)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=seconds)
        if t.is_alive():
            pytest.fail(
                f"call did not terminate within {seconds}s — likely cyclic walk"
            )
        if error:
            raise error[0]
        return result[0] if result else None

    def test_self_cycle_does_not_create_self_child(self) -> None:
        """A node whose parentUuid points to itself must not list itself as a child."""
        a = self._make_entry("a", parent_uuid=None, i=0)
        b = self._make_entry("b", parent_uuid="b", i=1)
        nodes = build_message_index([a, b])
        build_dag(nodes)
        assert "b" not in nodes["b"].children_uuids
        self._assert_no_child_cycle(nodes)

    def test_two_cycle_produces_acyclic_children(self) -> None:
        """A two-node cycle (A→B→A) must yield acyclic children_uuids."""
        a = self._make_entry("a", parent_uuid="b", i=0)
        b = self._make_entry("b", parent_uuid="a", i=1)
        nodes = build_message_index([a, b])
        build_dag(nodes)
        self._assert_no_child_cycle(nodes)

    def test_three_cycle_produces_acyclic_children(self) -> None:
        """A three-node cycle (A→B→C→A) must yield acyclic children_uuids."""
        a = self._make_entry("a", parent_uuid="c", i=0)
        b = self._make_entry("b", parent_uuid="a", i=1)
        c = self._make_entry("c", parent_uuid="b", i=2)
        nodes = build_message_index([a, b, c])
        build_dag(nodes)
        self._assert_no_child_cycle(nodes)

    def test_extract_session_dag_lines_terminates_on_self_cycle(self) -> None:
        """End-to-end: cyclic input must not hang extract_session_dag_lines."""
        a = self._make_entry("a", parent_uuid=None, i=0)
        b = self._make_entry("b", parent_uuid="b", i=1)
        nodes = build_message_index([a, b])
        build_dag(nodes)
        sessions = self._run_with_timeout(lambda: extract_session_dag_lines(nodes))
        assert sessions is not None
        assert "s1" in sessions
        assert set(sessions["s1"].uuids) == {"a", "b"}

    def test_extract_session_dag_lines_terminates_on_two_cycle(self) -> None:
        """End-to-end: two-node cycle must not hang extract_session_dag_lines."""
        a = self._make_entry("a", parent_uuid="b", i=0)
        b = self._make_entry("b", parent_uuid="a", i=1)
        nodes = build_message_index([a, b])
        build_dag(nodes)
        sessions = self._run_with_timeout(lambda: extract_session_dag_lines(nodes))
        assert sessions is not None
        assert "s1" in sessions
        assert set(sessions["s1"].uuids) == {"a", "b"}


# =============================================================================
# Test: build_dag cycle-check memoization (guard-transformation pin)
# =============================================================================


class TestCycleCheckMemoization:
    """Pin the ``acyclic`` memoization in ``build_dag``'s step-2 cycle check.

    The check memoizes nodes already known to reach a root so each node's
    ancestor chain is walked once overall (amortized O(n)) instead of
    re-walking the full chain per node (O(n·depth)). This is a *guard*
    transformation: memoizing must not weaken cycle detection nor the
    exactly-once cut. These tests fail if the memoization ever lets a
    cycle go uncut, cuts one more than once, re-cuts on a second chain
    entering an already-broken cycle, or disturbs the acyclic parent
    pointers on long shared-prefix chains.
    """

    _make = staticmethod(TestCyclicParentChain._make_entry)
    _assert_no_child_cycle = staticmethod(TestCyclicParentChain._assert_no_child_cycle)

    @staticmethod
    def _cycle_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.message for r in caplog.records if "Cycle detected" in r.message]

    def test_three_cycle_cut_exactly_once_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A→B→C→A is broken by nulling exactly one parent, with one warning."""
        a = self._make("a", parent_uuid="c", i=0)
        b = self._make("b", parent_uuid="a", i=1)
        c = self._make("c", parent_uuid="b", i=2)
        nodes = build_message_index([a, b, c])
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag(nodes)
        assert len(self._cycle_warnings(caplog)) == 1
        # Exactly one of the three cycle members had its parent nulled —
        # the memo set must not cause a second member to also be cut.
        nulled = [u for u in ("a", "b", "c") if nodes[u].parent_uuid is None]
        assert len(nulled) == 1
        self._assert_no_child_cycle(nodes)

    def test_chain_into_cut_cycle_does_not_recut(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A chain feeding into an already-broken cycle terminates, one cut only.

        The cycle a→b→c→a is cut once; the extra chain e→d→c enters it via
        the already-memoized ``c``. Without the memo (or with a broken one)
        the second walk could re-detect and re-cut the cycle; it must not.
        """
        a = self._make("a", parent_uuid="c", i=0)
        b = self._make("b", parent_uuid="a", i=1)
        c = self._make("c", parent_uuid="b", i=2)
        d = self._make("d", parent_uuid="c", i=3)
        e = self._make("e", parent_uuid="d", i=4)
        nodes = build_message_index([a, b, c, d, e])
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag(nodes)
        assert len(self._cycle_warnings(caplog)) == 1
        # The feeder chain keeps its (genuinely acyclic) parent pointers;
        # only the cycle's entry node was promoted to root.
        assert nodes["d"].parent_uuid == "c"
        assert nodes["e"].parent_uuid == "d"
        self._assert_no_child_cycle(nodes)

    def test_shared_prefix_chains_stay_acyclic_and_linked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Many leaves over one long ancestor prefix: no warning, links intact.

        This is the case the memo optimizes: r←a←b←c shared by five leaves.
        Each leaf's walk must stop at the first already-memoized ancestor,
        leave every parent pointer untouched, and yield correct child links.
        """
        r = self._make("r", parent_uuid=None, i=0)
        a = self._make("a", parent_uuid="r", i=1)
        b = self._make("b", parent_uuid="a", i=2)
        c = self._make("c", parent_uuid="b", i=3)
        leaves = [self._make(f"L{n}", parent_uuid="c", i=4 + n) for n in range(5)]
        nodes = build_message_index([r, a, b, c, *leaves])
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag(nodes)
        assert not self._cycle_warnings(caplog)
        # Parent pointers on a genuinely acyclic graph are never disturbed.
        assert nodes["a"].parent_uuid == "r"
        for n in range(5):
            assert nodes[f"L{n}"].parent_uuid == "c"
        # Child links reflect the shared prefix (insertion order preserved).
        assert nodes["r"].children_uuids == ["a"]
        assert nodes["a"].children_uuids == ["b"]
        assert nodes["b"].children_uuids == ["c"]
        assert nodes["c"].children_uuids == [f"L{n}" for n in range(5)]
        self._assert_no_child_cycle(nodes)

    def test_self_loop_cut_in_step2_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A self-loop (b→b) is broken with a warning; step-3 belt stays clean.

        Step 2 detects b re-appearing on its own one-node chain and nulls
        its parent, so the step-3 ``parent_uuid == uuid`` belt never fires.
        Either way b must not list itself as a child.
        """
        a = self._make("a", parent_uuid=None, i=0)
        b = self._make("b", parent_uuid="b", i=1)
        nodes = build_message_index([a, b])
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag(nodes)
        assert len(self._cycle_warnings(caplog)) == 1
        assert nodes["b"].parent_uuid is None
        assert "b" not in nodes["b"].children_uuids
        self._assert_no_child_cycle(nodes)


# =============================================================================
# Test: Degenerate parentUuid (all null)
# =============================================================================


class TestDegenerateParentUuid:
    """Test handling of entries where all parentUuid values are null.

    This is the common case for existing test data and older transcripts
    that lack parentUuid chains. The DAG should fall back to timestamp sort.
    """

    @pytest.fixture()
    def entries(self) -> list[TranscriptEntry]:
        """Create 5 entries with parentUuid=null, out of timestamp order."""
        data = [
            {
                "type": "user",
                "timestamp": f"2025-07-01T10:0{i}:00.000Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": f"msg_{i}",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"Message {i}"}],
                },
            }
            for i in [3, 1, 4, 0, 2]  # Out of order
        ]
        return [create_transcript_entry(d) for d in data]

    def test_all_null_parent_uuid_falls_back_to_timestamp(
        self, entries: list[TranscriptEntry]
    ) -> None:
        """All 5 entries with parentUuid=null should appear in timestamp order."""
        nodes = build_message_index(entries)
        build_dag(nodes)
        sessions = extract_session_dag_lines(nodes)

        dag_line = sessions["s1"]
        assert len(dag_line.uuids) == 5
        # Should be sorted by timestamp
        assert dag_line.uuids == ["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"]

    def test_all_null_traversal_returns_all_entries(
        self, entries: list[TranscriptEntry]
    ) -> None:
        """Traversal should return all 5 entries, none dropped."""
        tree = build_dag_from_entries(entries)
        result = traverse_session_tree(tree)
        assert len(result) == 5
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"]


# =============================================================================
# Test: Within-session fork (dag_within_fork.jsonl)
# =============================================================================


class TestWithinSessionFork:
    """Tests using dag_within_fork.jsonl: s1(a→b→c) with fork at c.

    c has two same-session children: d→e→f (branch 1) and d'→e' (branch 2).
    This produces three DAG-lines: trunk (a,b,c), branch 1 (d,e,f), branch 2 (d',e').
    """

    @pytest.fixture()
    def tree(self) -> SessionTree:
        entries = load_entries_from_jsonl(TEST_DATA / "dag_within_fork.jsonl")
        return build_dag_from_entries(entries)

    def test_trunk_stops_at_fork(self, tree: SessionTree) -> None:
        """Trunk DAG-line stops at fork point c."""
        assert "s1" in tree.sessions
        assert tree.sessions["s1"].uuids == ["a", "b", "c"]

    def test_branch_sessions_created(self, tree: SessionTree) -> None:
        """Two branch pseudo-sessions are created."""
        branch_ids = [sid for sid in tree.sessions if "@" in sid]
        assert len(branch_ids) == 2

    def test_branch1_chain(self, tree: SessionTree) -> None:
        """Branch 1 (d→e→f) has correct chain."""
        branch1_id = "s1@d"
        assert branch1_id in tree.sessions
        assert tree.sessions[branch1_id].uuids == ["d", "e", "f"]

    def test_branch2_chain(self, tree: SessionTree) -> None:
        """Branch 2 (d'→e') has correct chain."""
        branch2_id = "s1@d_prime"
        assert branch2_id in tree.sessions
        assert tree.sessions[branch2_id].uuids == ["d_prime", "e_prime"]

    def test_branches_are_marked(self, tree: SessionTree) -> None:
        """Branch DAG-lines have is_branch=True and original_session_id set."""
        for sid in tree.sessions:
            if "@" in sid:
                dl = tree.sessions[sid]
                assert dl.is_branch is True
                assert dl.original_session_id == "s1"
            else:
                dl = tree.sessions[sid]
                assert dl.is_branch is False
                assert dl.original_session_id is None

    def test_trunk_is_root(self, tree: SessionTree) -> None:
        """Only trunk s1 is a root session."""
        assert tree.roots == ["s1"]

    def test_branches_parent_is_trunk(self, tree: SessionTree) -> None:
        """Both branches have parent_session_id = trunk."""
        for sid in tree.sessions:
            if "@" in sid:
                assert tree.sessions[sid].parent_session_id == "s1"
                assert tree.sessions[sid].attachment_uuid == "c"

    def test_junction_at_fork_point(self, tree: SessionTree) -> None:
        """Fork point c is a junction point with both branches as targets."""
        assert "c" in tree.junction_points
        jp = tree.junction_points["c"]
        assert jp.session_id == "s1"
        assert len(jp.target_sessions) == 2
        assert "s1@d" in jp.target_sessions
        assert "s1@d_prime" in jp.target_sessions

    def test_traversal_order(self, tree: SessionTree) -> None:
        """Depth-first: trunk, then branch 1 at junction, then branch 2."""
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "e", "f", "d_prime", "e_prime"]

    def test_node_session_ids_updated(self, tree: SessionTree) -> None:
        """MessageNode.session_id is updated for branch nodes."""
        assert tree.nodes["a"].session_id == "s1"
        assert tree.nodes["b"].session_id == "s1"
        assert tree.nodes["c"].session_id == "s1"
        assert tree.nodes["d"].session_id == "s1@d"
        assert tree.nodes["e"].session_id == "s1@d"
        assert tree.nodes["f"].session_id == "s1@d"
        assert tree.nodes["d_prime"].session_id == "s1@d_prime"
        assert tree.nodes["e_prime"].session_id == "s1@d_prime"

    def test_traversal_covers_all_entries(self, tree: SessionTree) -> None:
        """All 8 entries should appear in traversal."""
        result = traverse_session_tree(tree)
        assert len(result) == 8


class TestNestedFork:
    """Test nested within-session forks (fork within a fork)."""

    def test_nested_fork(self) -> None:
        """Session with fork at b, then nested fork at d within first branch."""
        # a → b (fork) → d (fork) → f, g
        #             → e
        data = [
            {
                "type": "user",
                "timestamp": "2025-07-01T10:00:00.000Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "a",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Start"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-07-01T10:01:00.000Z",
                "parentUuid": "a",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "b",
                "requestId": "req_1",
                "message": {
                    "id": "b",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [{"type": "text", "text": "Fork point 1"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            # Branch 1 from b: c → d (fork)
            {
                "type": "user",
                "timestamp": "2025-07-01T10:02:00.000Z",
                "parentUuid": "b",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "c",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Branch 1"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2025-07-01T10:03:00.000Z",
                "parentUuid": "c",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "d",
                "requestId": "req_2",
                "message": {
                    "id": "d",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [{"type": "text", "text": "Fork point 2"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            # Nested branch 1a from d
            {
                "type": "user",
                "timestamp": "2025-07-01T10:04:00.000Z",
                "parentUuid": "d",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "f",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Nested branch 1a"}],
                },
            },
            # Nested branch 1b from d
            {
                "type": "user",
                "timestamp": "2025-07-01T10:05:00.000Z",
                "parentUuid": "d",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "g",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Nested branch 1b"}],
                },
            },
            # Branch 2 from b
            {
                "type": "user",
                "timestamp": "2025-07-01T10:06:00.000Z",
                "parentUuid": "b",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "e",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Branch 2"}],
                },
            },
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Trunk: a, b (stops at fork)
        assert tree.sessions["s1"].uuids == ["a", "b"]

        # Branch 1 from b: c, d (stops at nested fork)
        branch1_id = "s1@c"
        assert branch1_id in tree.sessions
        assert tree.sessions[branch1_id].uuids == ["c", "d"]

        # Nested branches from d (within branch 1)
        nested1a_id = f"{branch1_id}@f"
        nested1b_id = f"{branch1_id}@g"
        assert nested1a_id in tree.sessions
        assert tree.sessions[nested1a_id].uuids == ["f"]
        assert nested1b_id in tree.sessions
        assert tree.sessions[nested1b_id].uuids == ["g"]

        # Branch 2 from b
        branch2_id = "s1@e"
        assert branch2_id in tree.sessions
        assert tree.sessions[branch2_id].uuids == ["e"]

        # Traversal
        result = traverse_session_tree(tree)
        uuids = [e.uuid for e in result]  # type: ignore[union-attr]
        assert uuids == ["a", "b", "c", "d", "f", "g", "e"]


def _make_entry(
    etype: str,
    uuid: str,
    parent: str | None,
    ts: str,
    session: str = "s1",
    text: str = "",
) -> dict:
    """Helper to build a minimal transcript entry dict."""
    base = {
        "type": etype,
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session,
        "version": "1.0.0",
        "uuid": uuid,
    }
    if etype == "user":
        base["message"] = {
            "role": "user",
            "content": [{"type": "text", "text": text or uuid}],
        }
    elif etype == "assistant":
        base["requestId"] = f"req_{uuid}"
        base["message"] = {
            "id": uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text or uuid}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    elif etype == "system":
        base["message"] = {"content": []}
    return base


def _make_attachment(
    uuid: str,
    parent: str,
    ts: str,
    attachment_type: str = "hook_success",
    session: str = "s1",
) -> dict:
    """Helper to build an 'attachment' entry (becomes PassthroughTranscriptEntry)."""
    return {
        "type": "attachment",
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": session,
        "version": "1.0.0",
        "uuid": uuid,
        "attachment": {"type": attachment_type},
    }


class TestCompactionReplay:
    """Context compaction replays should not create branches."""

    def test_same_timestamp_children_not_forked(self) -> None:
        """Multiple children with identical timestamps are compaction replays."""
        # a → sys → replay1, replay2, replay3 (all same ts)
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("system", "sys", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("assistant", "r1", "sys", "2025-07-01T10:02:00.000Z"),
            _make_entry("assistant", "r2", "sys", "2025-07-01T10:02:00.000Z"),
            _make_entry("assistant", "r3", "sys", "2025-07-01T10:02:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Should be a single linear session, no branches
        assert "s1" in tree.sessions
        assert tree.sessions["s1"].uuids == ["a", "sys", "r1"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_different_timestamps_create_branches(self) -> None:
        """Children with different timestamps are real forks (rewinds)."""
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("user", "c", "b", "2025-07-01T10:02:00.000Z"),
            _make_entry("user", "d", "b", "2025-07-01T10:05:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Trunk stops at b, two branches
        assert tree.sessions["s1"].uuids == ["a", "b"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 2


class TestToolResultStitching:
    """Tool-result side-branches should be stitched into the main chain."""

    def test_single_tool_result_stitched(self) -> None:
        """A(tool_use) → U(result) + A(next) should become linear."""
        # a → tool_use → tool_result (dead end) + next_assistant
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("user", "result1", "tool1", "2025-07-01T10:01:00.100Z"),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:00.200Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Should be linear: a → tool1 → result1 → tool2
        assert tree.sessions["s1"].uuids == ["a", "tool1", "result1", "tool2"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_multiple_tool_results_stitched(self) -> None:
        """Multiple parallel tool_use with results should all be stitched."""
        # a → tool1 → result1 (dead end) + result2 (dead end) + tool2
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("user", "res1", "tool1", "2025-07-01T10:01:00.100Z"),
            _make_entry("user", "res2", "tool1", "2025-07-01T10:01:00.150Z"),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:00.200Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Should be linear: a → tool1 → res1 → res2 → tool2
        assert tree.sessions["s1"].uuids == ["a", "tool1", "res1", "res2", "tool2"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_user_child_with_continuation_stitched(self) -> None:
        """Variant 2: User child continues, Assistant subtree dead-ends.
        Should stitch linearly with dead-end first, then continuation."""
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("user", "c", "b", "2025-07-01T10:02:00.000Z"),
            _make_entry("assistant", "d", "b", "2025-07-01T10:03:00.000Z"),
            # c continues (not a dead end), d is a leaf (dead end)
            _make_entry("assistant", "e", "c", "2025-07-01T10:04:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Should stitch: dead-end d first, then continuing c
        assert tree.sessions["s1"].uuids == ["a", "b", "d", "c", "e"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_assistant_subtree_dead_end_stitched(self) -> None:
        """Variant 2 with deeper dead-end: Assistant child has a subtree
        that eventually terminates, User child continues."""
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            # b forks into assistant d (dead-end subtree) and user c (continues)
            _make_entry("assistant", "d", "b", "2025-07-01T10:02:00.000Z"),
            _make_entry("user", "c", "b", "2025-07-01T10:02:50.000Z"),
            # d's subtree: d → d2 → d3 (all dead ends)
            _make_entry("assistant", "d2", "d", "2025-07-01T10:02:01.000Z"),
            _make_entry("user", "d3", "d2", "2025-07-01T10:02:02.000Z"),
            # c continues the main chain
            _make_entry("assistant", "e", "c", "2025-07-01T10:03:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # d's subtree dead-ends, c continues: stitch d first, then c
        assert tree.sessions["s1"].uuids == ["a", "b", "d", "c", "e"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_parallel_tool_use_with_attachment_stitched(self) -> None:
        """Parallel tool_use: user(tool_result) has an attachment leaf,
        assistant sibling has conversation. Older 'no immediate child'
        check missed this; updated variant 1 uses _is_structural_subtree.
        Mirrors the 22 fake forks found in the BCT Teamcenter session.
        """
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            # tool1's children: user(tool_result) with attachment leaf
            # + next assistant tool_use (main chain)
            _make_entry("user", "res1", "tool1", "2025-07-01T10:01:00.100Z"),
            _make_attachment(
                "hook1", "res1", "2025-07-01T10:01:00.150Z", "hook_success"
            ),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:00.200Z"),
            _make_entry("user", "res2", "tool2", "2025-07-01T10:01:01.000Z"),
            _make_entry("assistant", "tool3", "tool2", "2025-07-01T10:01:02.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Should be linear: a → tool1 → res1 → tool2 → res2 → tool3
        # (hook1 is a descendant of res1 but passthrough, not rendered)
        assert tree.sessions["s1"].uuids == [
            "a",
            "tool1",
            "res1",
            "tool2",
            "res2",
            "tool3",
        ]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0


def _with_agent_id(entry: dict, agent_id: str) -> dict:
    """Attach an agentId to a user entry (marks it as a subagent anchor)."""
    entry["agentId"] = agent_id
    return entry


class TestParallelAgentAnchorPreservation:
    """Parallel Task/Agent tool_uses produce sibling tool_result anchors.

    When the stitch logic classifies an assistant sibling subtree as
    dead-end, any UserTranscriptEntry.agentId inside that subtree is the
    attachment point for a subagent session. Those anchors must survive in
    the main DAG-line or the subagent sessions can't be spliced in.
    """

    def test_inner_anchor_preserved_in_dead_end_subtree(self) -> None:
        """Two parallel Agent tool_uses, inner anchor inside dead-end sibling.

        Shape::

            a → tool1 → tool2 → res2(agentId="agent-2")   [inner anchor]
                     → res1(agentId="agent-1")
                        → att1 → cont                    [main continues]

        At the tool1 fork, variant 2 picks res1 as the continuation and
        tool2 as the dead-end sibling. Without the fix, res2 gets collected
        as a skipped descendant of tool2, breaking the agent-2 attachment.
        """
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:01.000Z"),
            _with_agent_id(
                _make_entry("user", "res2", "tool2", "2025-07-01T10:02:00.000Z"),
                "agent-2",
            ),
            _with_agent_id(
                _make_entry("user", "res1", "tool1", "2025-07-01T10:02:10.000Z"),
                "agent-1",
            ),
            _make_attachment("att1", "res1", "2025-07-01T10:02:11.000Z"),
            _make_entry("assistant", "cont", "att1", "2025-07-01T10:03:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Both anchors must appear in the main DAG-line
        uuids = tree.sessions["s1"].uuids
        assert "res1" in uuids, "outer anchor dropped"
        assert "res2" in uuids, "inner anchor (agent-2) dropped — fix regression"
        # No spurious branches — stitch should produce one linear DAG-line
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_three_parallel_agents_all_anchors_preserved(self) -> None:
        """Three parallel Agent tool_uses (the teammates shape).

        Shape mirrors ``experiments/worktrees``: 3 chained Agent tool_uses
        with tool_results that dead-end at each level, and the main chain
        continues via an attachment after the outer tool_result.
        """
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:01.000Z"),
            _make_entry("assistant", "tool3", "tool2", "2025-07-01T10:01:02.000Z"),
            _with_agent_id(
                _make_entry("user", "res3", "tool3", "2025-07-01T10:02:00.000Z"),
                "agent-3",
            ),
            _with_agent_id(
                _make_entry("user", "res2", "tool2", "2025-07-01T10:02:10.000Z"),
                "agent-2",
            ),
            _with_agent_id(
                _make_entry("user", "res1", "tool1", "2025-07-01T10:02:20.000Z"),
                "agent-1",
            ),
            _make_attachment("att1", "res1", "2025-07-01T10:02:21.000Z"),
            _make_entry("assistant", "cont", "att1", "2025-07-01T10:03:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        uuids = set(tree.sessions["s1"].uuids)
        missing = {"res1", "res2", "res3"} - uuids
        assert not missing, f"anchors missing from main DAG-line: {missing}"

    def test_anchor_attachment_routes_subagent_session(self) -> None:
        """End-to-end: anchor-bearing tool_result enables subagent splice.

        Builds main + a sidechain session whose entries use ``res2`` as the
        anchor via ``parentUuid``. After the fix, ``traverse_session_tree``
        must yield the subagent messages interleaved at the anchor point.
        """
        from claude_code_log.converter import _integrate_agent_entries

        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "tool1", "a", "2025-07-01T10:01:00.000Z"),
            _make_entry("assistant", "tool2", "tool1", "2025-07-01T10:01:01.000Z"),
            _with_agent_id(
                _make_entry("user", "res2", "tool2", "2025-07-01T10:02:00.000Z"),
                "agent-2",
            ),
            _with_agent_id(
                _make_entry("user", "res1", "tool1", "2025-07-01T10:02:10.000Z"),
                "agent-1",
            ),
            _make_attachment("att1", "res1", "2025-07-01T10:02:11.000Z"),
            _make_entry("assistant", "cont", "att1", "2025-07-01T10:03:00.000Z"),
        ]
        # Sidechain entries for agent-2 (parentUuid=None, will be reparented
        # to the res2 anchor by _integrate_agent_entries).
        sub = {
            "type": "user",
            "timestamp": "2025-07-01T10:02:05.000Z",
            "parentUuid": None,
            "isSidechain": True,
            "agentId": "agent-2",
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "sub1",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        data.append(sub)

        entries = [create_transcript_entry(d) for d in data]
        _integrate_agent_entries(entries)
        tree = build_dag_from_entries(entries)
        traversed_uuids = [getattr(e, "uuid", "") for e in traverse_session_tree(tree)]
        assert "sub1" in traversed_uuids, (
            "subagent entry not reached via anchor — agent session not spliced"
        )


class TestStructuralOnlyFork:
    """All-passthrough children should collapse instead of creating forks."""

    def test_attachment_only_children_terminate(self) -> None:
        """Parent with only attachment children (e.g. hook_success +
        SessionStart:resume at different times) should not fork."""
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            # b has two attachment children with different timestamps.
            _make_attachment("att1", "b", "2025-07-01T10:02:00.000Z", "hook_success"),
            _make_attachment(
                "att2",
                "b",
                "2025-07-02T09:00:00.000Z",
                "hook_success",
            ),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Linear trunk with both attachments collapsed in, no branches
        assert tree.sessions["s1"].uuids == ["a", "b", "att1", "att2"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_real_within_session_fork_preserved(self) -> None:
        """Two children with conversational subtrees at different times
        remain a real fork (user /fork-style rewind)."""
        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            # b has two live children, each with conversation
            _make_entry("user", "u1", "b", "2025-07-01T10:02:00.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:02:30.000Z"),
            _make_entry("user", "u2", "b", "2025-07-01T10:10:00.000Z"),
            _make_entry("assistant", "a2", "u2", "2025-07-01T10:10:30.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        branches = [s for s in tree.sessions.values() if s.is_branch]
        assert len(branches) == 2
        branch_uuids = {tuple(b.uuids) for b in branches}
        assert ("u1", "a1") in branch_uuids
        assert ("u2", "a2") in branch_uuids


def _make_system_entry(
    uuid: str,
    parent: str | None,
    ts: str,
    subtype: str = "info",
    session: str = "s1",
    content: str = "",
    compactMetadata: dict | None = None,
) -> dict:
    """Helper to build a minimal system transcript entry dict."""
    entry: dict = {
        "type": "system",
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session,
        "version": "1.0.0",
        "uuid": uuid,
        "subtype": subtype,
        "content": content,
    }
    if compactMetadata is not None:
        entry["compactMetadata"] = compactMetadata
    return entry


class TestRootClassification:
    """Multi-root sessions should warn only on unexpected root types."""

    def test_compact_boundary_roots_are_expected(self, caplog) -> None:
        """Two compact_boundary roots alongside a system local_command root
        are all expected; no WARNING should fire."""
        import logging

        data = [
            # Root 1: a /memory-like local_command at session start
            _make_system_entry(
                "lc",
                None,
                "2025-07-01T10:00:00.000Z",
                subtype="local_command",
            ),
            _make_entry("user", "u0", "lc", "2025-07-01T10:00:01.000Z"),
            # Root 2: first compact boundary
            _make_system_entry(
                "cb1",
                None,
                "2025-07-01T11:00:00.000Z",
                subtype="compact_boundary",
            ),
            _make_entry("user", "u1", "cb1", "2025-07-01T11:00:01.000Z"),
            # Root 3: second compact boundary
            _make_system_entry(
                "cb2",
                None,
                "2025-07-01T12:00:00.000Z",
                subtype="compact_boundary",
            ),
            _make_entry("user", "u2", "cb2", "2025-07-01T12:00:01.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"Expected no warnings, got: {[r.message for r in warnings]}"
        )

    def test_unexpected_root_still_warns(self, caplog) -> None:
        """An orphan user entry (parent missing) triggers a warning."""
        import logging

        data = [
            _make_entry("user", "a", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "b", "a", "2025-07-01T10:01:00.000Z"),
            # Orphan user with parent pointing outside the session.
            _make_entry("user", "orphan", "not-in-session", "2025-07-01T11:00:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        # Orphan promotion emits its own warning; the multi-root warning
        # should also fire because the orphan-promoted root is a user
        # entry (unexpected type).
        multi_root_warnings = [r for r in warnings if "roots found" in r.message]
        assert multi_root_warnings, (
            f"Expected a multi-root warning; got: {[r.message for r in warnings]}"
        )

    def test_progress_passthrough_roots_are_expected(self, caplog) -> None:
        """`progress` passthrough roots — both the SessionStart shape
        (parentUuid:null naturally) and the orphan-promoted shape (a
        PostToolUse hook whose spawning tool_use was compacted away) —
        must NOT trigger the multi-root warning. Both are routine
        async-hook artifacts; treating them as expected matches the
        reality on long-running real-world sessions."""
        import logging

        def _passthrough(uuid: str, parent: str | None, ts: str) -> dict:
            return {
                "type": "progress",
                "timestamp": ts,
                "parentUuid": parent,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": uuid,
            }

        data = [
            # Root 1: SessionStart hook firing before any user turn
            # (parentUuid:null naturally).
            _passthrough("p_start", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("user", "u1", "p_start", "2025-07-01T10:00:01.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            # Compact in the middle.
            _make_system_entry(
                "cb",
                None,
                "2025-07-01T11:00:00.000Z",
                subtype="compact_boundary",
            ),
            _make_entry("user", "u2", "cb", "2025-07-01T11:00:01.000Z"),
            # Root 3: a PostToolUse-shaped passthrough whose parent
            # uuid points outside the session (compacted-out tool_use).
            _passthrough("p_orphan", "missing-parent", "2025-07-01T10:59:55.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        # The orphan-promotion warning (one per orphan, fires from
        # `build_dag` for any missing parent) is fine — it's the
        # multi-root "X roots found (Y unexpected)" warning that
        # progress passthroughs must NOT trigger.
        multi_root_warnings = [r for r in warnings if "roots found" in r.message]
        assert not multi_root_warnings, (
            f"Expected no multi-root warning for progress passthroughs; "
            f"got: {[r.message for r in multi_root_warnings]}"
        )

    def test_orphan_sidechain_root_no_agent_id_is_expected(self, caplog) -> None:
        """A sidechain user with parentUuid=None and no agentId is the
        Task-prompt shape from older Claude Code versions that didn't
        record agentId. Loaded without its trunk anchor it surfaces as
        a multi-root, but the pattern is routine — ``--resume`` of an
        old transcript or partially-loaded data — so it should not
        trigger the warning. Real-world repro: see the ``bf36f743``
        session in the user's repower project."""
        import logging

        sub = {
            "type": "user",
            "timestamp": "2025-09-28T16:29:26.782Z",
            "parentUuid": None,
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "sub_root",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        data = [
            _make_entry("user", "u1", None, "2025-09-28T16:28:41.791Z"),
            _make_entry("assistant", "a1", "u1", "2025-09-28T16:28:50.000Z"),
            sub,
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        multi_root_warnings = [r for r in warnings if "roots found" in r.message]
        assert not multi_root_warnings, (
            f"orphan sidechain root (no agentId) must not warn; "
            f"got: {[r.message for r in multi_root_warnings]}"
        )

    def test_genuine_user_start_with_compact_boundaries_does_not_warn(
        self, caplog
    ) -> None:
        """The earliest user root is the genuine session start. Treating
        it as 'unexpected' alongside expected ``compact_boundary`` roots
        produces noise on every long session. Real-world repro: the
        ``8f68ac90`` session has 1 user start + 3 compact boundaries."""
        import logging

        data = [
            # Genuine session start: earliest user with parentUuid=None
            _make_entry("user", "u0", None, "2025-11-12T12:59:17.970Z"),
            _make_entry("assistant", "a0", "u0", "2025-11-12T13:00:00.000Z"),
            # Three /compact boundaries through the day
            _make_system_entry(
                "cb1", None, "2025-11-12T16:12:59.612Z", subtype="compact_boundary"
            ),
            _make_entry("user", "u1", "cb1", "2025-11-12T16:13:00.000Z"),
            _make_system_entry(
                "cb2", None, "2025-11-12T17:18:26.261Z", subtype="compact_boundary"
            ),
            _make_entry("user", "u2", "cb2", "2025-11-12T17:18:27.000Z"),
            _make_system_entry(
                "cb3", None, "2025-11-12T18:05:00.843Z", subtype="compact_boundary"
            ),
            _make_entry("user", "u3", "cb3", "2025-11-12T18:05:01.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        multi_root_warnings = [r for r in warnings if "roots found" in r.message]
        assert not multi_root_warnings, (
            f"genuine user start + compact_boundaries must not warn; "
            f"got: {[r.message for r in multi_root_warnings]}"
        )

    def test_cross_session_attachment_root_is_expected(self, caplog) -> None:
        """An entry whose ``parentUuid`` resolves to a node in another
        loaded session is a legitimate cross-session attachment (typical
        ``--resume`` shape, where the resumed session's transcript
        replays history under the new sessionId). The local session
        sees it as a 'root' (parent isn't in *its* uuids), but the
        parent does exist in the loaded data. Real-world repro: the
        ``ffc4b7ae`` session attaches at multiple points to the earlier
        ``d1a8cc99`` session it resumed."""
        import logging

        # Session s1 with normal chain a→b→c
        s1 = [
            _make_entry("user", "a", None, "2025-08-16T00:04:00.000Z", session="s1"),
            _make_entry(
                "assistant", "b", "a", "2025-08-16T00:05:00.000Z", session="s1"
            ),
            _make_entry("user", "c", "b", "2025-08-16T00:06:00.000Z", session="s1"),
        ]
        # Session s2 starts fresh, but later entries attach to s1's `b` and `c`
        s2 = [
            _make_entry("user", "x", None, "2025-08-16T00:42:00.000Z", session="s2"),
            _make_entry(
                "assistant", "y", "x", "2025-08-16T00:43:00.000Z", session="s2"
            ),
            # Cross-session attachment: parent `b` belongs to s1
            _make_entry("user", "z1", "b", "2025-08-16T00:44:00.000Z", session="s2"),
            # Cross-session attachment: parent `c` belongs to s1
            _make_entry("user", "z2", "c", "2025-08-16T00:45:00.000Z", session="s2"),
        ]
        entries = [create_transcript_entry(d) for d in (s1 + s2)]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        s2_multi_root = [
            r for r in warnings if "roots found" in r.message and "s2" in r.message
        ]
        assert not s2_multi_root, (
            f"cross-session attachment roots must not warn; "
            f"got: {[r.message for r in s2_multi_root]}"
        )

    def test_sidechain_orphan_earlier_than_user_start_does_not_warn(
        self, caplog
    ) -> None:
        """A sidechain orphan's timestamp can land *before* the genuine
        non-sidechain user start (e.g. when a `--resume` session
        replays an old Task prompt from a prior session). The genuine
        start must still be the earliest non-sidechain natural root,
        not the chronologically-first sidechain. Real-world repro: the
        ``25217827`` session has a sidechain orphan dated 2 days
        before the actual session-start user message."""
        import logging

        early_sidechain = {
            "type": "user",
            "timestamp": "2025-10-26T17:11:57.584Z",
            "parentUuid": None,
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "old_sub",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        data = [
            early_sidechain,
            _make_entry("user", "u1", None, "2025-10-28T16:57:26.077Z"),
            _make_entry("assistant", "a1", "u1", "2025-10-28T16:58:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        with caplog.at_level(logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        multi_root_warnings = [r for r in warnings if "roots found" in r.message]
        assert not multi_root_warnings, (
            f"sidechain orphan earlier than user start must not warn; "
            f"got: {[r.message for r in multi_root_warnings]}"
        )


class TestMixedStructuralCollapse:
    """A structural (passthrough) sibling of a conversational child should
    collapse into the chain rather than creating a spurious 1-branch fork.
    Mirrors the `<progress>` sibling pattern observed in real sessions."""

    def test_user_plus_progress_collapses(self) -> None:
        """assistant parent with [user, progress-leaf] children → linear chain."""
        data = [
            _make_entry("user", "u1", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            # Live user child (continues the conversation)
            _make_entry("user", "u2", "a1", "2025-07-01T10:02:00.000Z"),
            # Structural progress sibling with a different timestamp
            {
                "type": "progress",
                "timestamp": "2025-07-01T10:01:30.000Z",
                "parentUuid": "a1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "p1",
            },
            _make_entry("assistant", "a2", "u2", "2025-07-01T10:03:00.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Linear chain — progress stitched as dead-end, conversation continues.
        assert tree.sessions["s1"].uuids == ["u1", "a1", "p1", "u2", "a2"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_progress_chain_collapses(self) -> None:
        """A chain of structural passthroughs (progress→progress) still
        counts as structural and collapses alongside a live sibling."""
        data = [
            _make_entry("user", "u1", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            _make_entry("user", "u2", "a1", "2025-07-01T10:02:00.000Z"),
            # Progress chain (structural subtree with no user/assistant descendants)
            {
                "type": "progress",
                "timestamp": "2025-07-01T10:01:30.000Z",
                "parentUuid": "a1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "p1",
            },
            {
                "type": "progress",
                "timestamp": "2025-07-01T10:01:45.000Z",
                "parentUuid": "p1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": "p2",
            },
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # p2 is a descendant of p1 — only p1 is stitched into the chain as a
        # dead-end; p2 is collected into `skipped`. Live path is u1→a1→p1→u2.
        uuids = tree.sessions["s1"].uuids
        assert uuids == ["u1", "a1", "p1", "u2"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_real_fork_not_collapsed(self) -> None:
        """If both children carry conversation, a real fork is preserved."""
        data = [
            _make_entry("user", "u1", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            # Two conversational children with distinct timestamps.
            _make_entry("user", "u2", "a1", "2025-07-01T10:02:00.000Z"),
            _make_entry("assistant", "a2", "u2", "2025-07-01T10:02:30.000Z"),
            _make_entry("user", "u3", "a1", "2025-07-01T10:10:00.000Z"),
            _make_entry("assistant", "a3", "u3", "2025-07-01T10:10:30.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        branches = [s for s in tree.sessions.values() if s.is_branch]
        assert len(branches) == 2


class TestParallelToolUseViaPassthrough:
    """Parallel-tool_use chains threaded through ``progress`` passthroughs.

    Real Claude Code teammate transcripts emit each parallel tool_use under
    its own assistant message, then chain them together via `progress`
    passthrough callbacks rather than as direct siblings. The dead-end
    user(tool_result) for the first parallel tool_use sits beside that
    passthrough chain, producing a 2-child fork at every parallel turn:

        A(tool_use₁)
        ├── U(tool_result₁) → progress (structural, dead end)
        └── progress → A(tool_use₂) → U(tool_result₂) → progress → A(tool_use₃) → ...

    The passthrough subtree carries the live continuation; the
    user(tool_result) carries only structural callbacks. Without the
    Variant 3 stitch each turn becomes a spurious 2-branch fork.
    """

    def _passthrough(self, uuid: str, parent: str, ts: str) -> dict:
        return {
            "type": "progress",
            "timestamp": ts,
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": uuid,
        }

    def test_passthrough_with_live_subtree_collapses(self) -> None:
        """user(tool_result) subtree is structural, passthrough chain is live."""
        data = [
            _make_entry("user", "u1", None, "2025-07-01T10:00:00.000Z"),
            # Assistant emits two parallel tool_uses.
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            # First tool_result + its dead-end progress callback.
            _make_entry("user", "tr1", "a1", "2025-07-01T10:01:02.000Z"),
            self._passthrough("p_tr1", "tr1", "2025-07-01T10:01:02.500Z"),
            # Live progress chain leading to the second parallel tool_use.
            self._passthrough("p_chain", "a1", "2025-07-01T10:01:01.000Z"),
            _make_entry("assistant", "a2", "p_chain", "2025-07-01T10:01:03.000Z"),
            _make_entry("user", "tr2", "a2", "2025-07-01T10:01:04.000Z"),
            _make_entry("assistant", "a3", "tr2", "2025-07-01T10:01:05.000Z"),
        ]
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # Spurious fork elided: trunk threads through the passthrough chain.
        # tr1 is appended to the trunk as a dead-end side entry; its
        # passthrough descendant is collected into ``skipped`` and absent.
        uuids = tree.sessions["s1"].uuids
        assert uuids == ["u1", "a1", "tr1", "p_chain", "a2", "tr2", "a3"]
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0

    def test_deep_passthrough_chain_classified_structural(self) -> None:
        """Exercises the depth-unboundedness invariant Variant 3 *depends
        on*, not Variant 3 itself. Live continuation here flows through
        the user child ``u2`` (the structural-side-branch collapse path —
        Shape B in dev-docs/dag.md), with a >20-deep ``progress`` chain
        as the structural sibling.

        ``_is_structural_subtree`` no longer clamps at depth=20 — the
        ``seen`` set + ``session_uuids`` filter still bound termination,
        but the depth cap previously misclassified long passthrough
        chains as live (returning ``False`` for "too deep to tell"),
        which suppressed both this Shape B collapse and Variant 3.
        Without the cap removal a parallel-tool_use anchor whose
        passthrough sibling unfolds into >20 chained ``progress``
        callbacks would falsely appear "live" — leaving a spurious
        1-branch fork.
        """
        data = [
            _make_entry("user", "u1", None, "2025-07-01T10:00:00.000Z"),
            _make_entry("assistant", "a1", "u1", "2025-07-01T10:01:00.000Z"),
            # Live continuation through the user child.
            _make_entry("user", "u2", "a1", "2025-07-01T10:02:00.000Z"),
        ]
        # Sibling passthrough chain of 25 entries, all structural.
        prev = "a1"
        for i in range(25):
            uuid = f"p{i:02d}"
            data.append(
                self._passthrough(uuid, prev, f"2025-07-01T10:01:{30 + i:02d}.000Z")
            )
            prev = uuid
        entries = [create_transcript_entry(d) for d in data]
        tree = build_dag_from_entries(entries)

        # The deep passthrough chain is structural; the existing
        # passthrough-collapse path absorbs p00 (and skips its descendants),
        # the chain follows u2.
        uuids = tree.sessions["s1"].uuids
        assert "u1" in uuids and "a1" in uuids and "u2" in uuids
        assert "p00" in uuids  # stitched in as the structural sibling
        branch_count = sum(1 for s in tree.sessions.values() if s.is_branch)
        assert branch_count == 0


# =============================================================================
# Test: Orphan-subagent self-anchor regression
# =============================================================================


class TestOrphanSubagentNoSelfAnchor:
    """Regression: an orphan subagent transcript must not anchor to itself.

    ``_integrate_agent_entries`` re-parents a sidechain root (parentUuid=None)
    to the trunk tool_result that spawned that agentId. Earlier versions
    accepted *any* sidechain entry as a fallback anchor, so when the trunk
    transcript wasn't loaded the agent's own root entry got registered as
    its own anchor — the subsequent re-parent step then set
    ``parentUuid = uuid`` and ``build_dag`` raised "Cycle detected".

    See ``test_data/dag_cycle.jsonl`` for a real-world repro from the
    claude-code-log project's own logs (single sidechain assistant entry,
    no trunk anchor anywhere).
    """

    def test_dag_cycle_fixture_no_self_loop(self) -> None:
        """Loading the orphan-subagent fixture must not produce self-loops.

        The fixture is a one-line JSONL with a sidechain assistant entry
        whose parentUuid is null and whose agentId has no matching trunk.
        After ``_integrate_agent_entries``, the entry's parentUuid must
        not equal its own uuid.
        """
        from claude_code_log.converter import _integrate_agent_entries

        entries = load_entries_from_jsonl(TEST_DATA / "dag_cycle.jsonl")
        assert len(entries) == 1, "fixture should be a single orphan sidechain entry"

        _integrate_agent_entries(entries)

        entry = entries[0]
        uuid = getattr(entry, "uuid", None)
        parent_uuid = getattr(entry, "parentUuid", None)
        assert uuid is not None
        assert parent_uuid != uuid, (
            f"orphan subagent re-parented to itself: parentUuid={parent_uuid} "
            f"uuid={uuid} — would create a self-loop in the DAG"
        )

    def test_dag_cycle_fixture_emits_no_cycle_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End-to-end: building the DAG from the fixture emits no cycle warning."""
        import logging as _logging

        from claude_code_log.converter import _integrate_agent_entries

        entries = load_entries_from_jsonl(TEST_DATA / "dag_cycle.jsonl")
        _integrate_agent_entries(entries)

        with caplog.at_level(_logging.WARNING, logger="claude_code_log.dag"):
            build_dag_from_entries(entries)

        cycle_warnings = [r for r in caplog.records if "Cycle detected" in r.message]
        assert not cycle_warnings, (
            f"unexpected cycle warning(s): {[r.message for r in cycle_warnings]}"
        )

    def test_nested_agent_anchor_in_sidechain_still_resolves(self) -> None:
        """Nested anchor (A's sidechain spawns B) must still re-parent B's root.

        Guards against over-tightening the fix: the legitimate sidechain
        anchor case — where a parent agent A's tool_result inside its own
        sidechain references a child agent B (different agentId) — must
        keep working. A's chain entries have agentId='A'; the B-spawning
        tool_result inside A has agentId='B' and a parent with agentId='A'.
        That's the cross-agent boundary that qualifies it as a true anchor.
        """
        from claude_code_log.converter import _integrate_agent_entries

        # Agent A's sidechain transcript: root + tool_use + tool_result anchor for B
        a_root = {
            "type": "user",
            "timestamp": "2025-07-01T10:00:00.000Z",
            "parentUuid": None,
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "a_root",
            "agentId": "agent-A",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi A"}]},
        }
        a_tool = {
            "type": "assistant",
            "timestamp": "2025-07-01T10:01:00.000Z",
            "parentUuid": "a_root",
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "a_tool",
            "agentId": "agent-A",
            "requestId": "r1",
            "message": {
                "id": "a_tool",
                "type": "message",
                "role": "assistant",
                "model": "claude-3",
                "content": [{"type": "text", "text": "spawning B"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        # tool_result anchor for B inside A's sidechain — agentId differs from A
        b_anchor = {
            "type": "user",
            "timestamp": "2025-07-01T10:02:00.000Z",
            "parentUuid": "a_tool",
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "b_anchor",
            "agentId": "agent-B",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "B done"}],
            },
        }
        # Agent B's own sidechain root (parentUuid=None, agentId=B)
        b_root = {
            "type": "user",
            "timestamp": "2025-07-01T10:01:30.000Z",
            "parentUuid": None,
            "isSidechain": True,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "1.0.0",
            "uuid": "b_root",
            "agentId": "agent-B",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi B"}]},
        }
        entries = [
            create_transcript_entry(d) for d in (a_root, a_tool, b_anchor, b_root)
        ]
        _integrate_agent_entries(entries)

        # B's root should be re-parented to b_anchor (not to itself or to b_root)
        b_root_entry = next(e for e in entries if getattr(e, "uuid", "") == "b_root")
        assert b_root_entry.parentUuid == "b_anchor", (
            f"nested anchor not honoured: b_root.parentUuid={b_root_entry.parentUuid}"
        )
        # A's root is the only remaining true root for agent-A and has no
        # trunk anchor — it should stay as a root, not self-loop.
        a_root_entry = next(e for e in entries if getattr(e, "uuid", "") == "a_root")
        assert a_root_entry.parentUuid is None, (
            f"agent-A root self-loop: parentUuid={a_root_entry.parentUuid}"
        )

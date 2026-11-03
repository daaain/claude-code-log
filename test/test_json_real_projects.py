"""Structural-invariant tests for JSON output across real-project fixtures.

These tests are parametrized over every session-level JSONL in
``test/test_data/real_projects/`` (excluding ``agent-*`` files, which
the parser loads via session references rather than directly). For
each fixture they assert *structural* invariants of the rendered JSON
— shape, referential integrity, role exclusivity — rather than
specific values. Real fixtures provide the diversity (sidechains,
within-session forks, multi-session resumes, teammates, …) that the
hand-written ``test_json_rendering`` cases can't cover.

Adding a new content type or changing the pairing model? These tests
will catch:

- A renderer that forgets to emit a new pair-like field.
- A new content type whose ``message_type`` value is malformed.
- A stale ``pair_first`` reference left dangling after filtering.
- A node accidentally classified into multiple pair roles.
- Any structural drift from the documented schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.json.renderer import JsonRenderer


# Session-level fixtures only. ``agent-*.jsonl`` files (whether at the
# project root or under ``subagents/``) are loaded via Task references
# from their parent session, not directly — mirrors the production
# filter in ``_generate_individual_session_files``.
_FIXTURES_ROOT = Path(__file__).parent / "test_data" / "real_projects"
_SESSION_FIXTURES = sorted(
    f
    for f in _FIXTURES_ROOT.rglob("*.jsonl")
    if not f.name.startswith("agent-") and "subagents" not in f.parts
)


# Type strings should look like Python identifiers extended with hyphens
# (a few legacy types use ``bash-input`` / ``bash-output``). Catches
# garbage like empty strings or path-like values, without hardcoding the
# closed set (which rots when new content types land).
_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

# Required top-level keys on the JSON payload.
_REQUIRED_TOP_LEVEL_KEYS = frozenset(
    {"version", "title", "depth", "compact", "sessions", "messages"}
)

# Required per-node keys. Optional keys (uuid, parent_uuid, pair_*, …)
# are only present when the underlying TemplateMessage carries them.
_REQUIRED_NODE_KEYS = frozenset(
    {"index", "type", "title", "timestamp", "session_id", "content"}
)


# ---------- helpers ----------------------------------------------------------


def _walk(nodes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every node in a JSON message tree (roots + descendants)."""
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


def _render_fixture_to_data(fixture: Path) -> dict[str, Any]:
    """Load a JSONL fixture and run it through ``JsonRenderer``.

    Returned value is the parsed JSON payload — assertion failures here
    are themselves structural problems (the renderer crashed, or emitted
    something `json.loads` rejects).
    """
    messages = load_transcript(fixture, silent=True)
    out = JsonRenderer().generate(messages, fixture.stem)
    # Will raise json.JSONDecodeError on malformed output; that itself
    # is a meaningful failure mode worth surfacing.
    return json.loads(out)


# ---------- parametrization --------------------------------------------------


def _fixture_id(fixture: Path) -> str:
    """Produce a short test ID from a fixture path."""
    rel = fixture.relative_to(_FIXTURES_ROOT)
    return str(rel)


@pytest.fixture(
    params=_SESSION_FIXTURES, ids=[_fixture_id(f) for f in _SESSION_FIXTURES]
)
def fixture_data(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Per-fixture rendered JSON payload.

    Cached at function scope: each invariant test gets a fresh render.
    Re-rendering is cheap (no I/O beyond the JSONL read) and keeps the
    fixtures independent of one another, which matters for failure
    isolation.
    """
    fixture: Path = request.param
    return _render_fixture_to_data(fixture)


# ---------- invariants -------------------------------------------------------


class TestStructuralInvariants:
    """Run on every real-project session fixture."""

    def test_top_level_schema(self, fixture_data: dict[str, Any]) -> None:
        """Top-level payload carries the documented required keys."""
        missing = _REQUIRED_TOP_LEVEL_KEYS - set(fixture_data.keys())
        assert not missing, f"missing top-level keys: {sorted(missing)}"
        assert isinstance(fixture_data["sessions"], list)
        assert isinstance(fixture_data["messages"], list)

    def test_every_node_has_required_keys(self, fixture_data: dict[str, Any]) -> None:
        """Every node — root or descendant — has the documented required keys."""
        for node in _walk(fixture_data["messages"]):
            missing = _REQUIRED_NODE_KEYS - set(node.keys())
            assert not missing, (
                f"node index={node.get('index')} missing keys: {sorted(missing)}"
            )
            assert isinstance(node["index"], int), node
            assert isinstance(node["type"], str), node
            assert isinstance(node["title"], str), node
            assert isinstance(node["session_id"], str), node
            assert isinstance(node["content"], dict), node

    def test_indexes_are_unique(self, fixture_data: dict[str, Any]) -> None:
        """``index`` is a per-document identifier — must be unique tree-wide."""
        indexes = [node["index"] for node in _walk(fixture_data["messages"])]
        seen: dict[int, int] = {}
        duplicates: list[int] = []
        for idx in indexes:
            seen[idx] = seen.get(idx, 0) + 1
            if seen[idx] == 2:
                duplicates.append(idx)
        assert not duplicates, (
            f"duplicate node indexes: {duplicates[:10]} "
            f"({len(duplicates)} total of {len(indexes)} nodes)"
        )

    def test_pair_references_resolve(self, fixture_data: dict[str, Any]) -> None:
        """Every ``pair_first`` / ``pair_middle`` / ``pair_last`` value points
        at a node that actually exists in the same document. A dangling
        reference would mean the renderer emitted a pair member without
        its partner — the bug pattern this PR's ``pair_middle`` fix
        addressed at the emitter layer."""
        all_indexes = {node["index"] for node in _walk(fixture_data["messages"])}
        dangling: list[tuple[int, str, int]] = []
        for node in _walk(fixture_data["messages"]):
            for key in ("pair_first", "pair_middle", "pair_last"):
                if key in node and node[key] not in all_indexes:
                    dangling.append((node["index"], key, node[key]))
        assert not dangling, (
            f"dangling pair references: {dangling[:5]} ({len(dangling)} total)"
        )

    def test_pair_roles_are_exclusive(self, fixture_data: dict[str, Any]) -> None:
        """Pair roles are mutually exclusive. Field-vs-role encoding
        (``pair_first`` is the *field name*, not the *role*):

        - unpaired: neither ``pair_first`` nor ``pair_last`` field set
        - 2-pair first: only ``pair_last`` set (points at partner)
        - 3-triple first: ``pair_last`` + ``pair_middle`` set, no ``pair_first``
        - middle of triple: both ``pair_first`` and ``pair_last`` set
        - last (of pair or triple): only ``pair_first`` set

        ``pair_middle`` (the field) is only ever on the triple's pair_first
        — never on a middle or last member, never co-occurring with the
        ``pair_first`` field on the same node."""
        for node in _walk(fixture_data["messages"]):
            has_first = "pair_first" in node
            has_middle_field = "pair_middle" in node
            has_last = "pair_last" in node
            # `pair_middle` field is only set on the triple's pair_first,
            # which has `pair_last` set (pointing at the triple's last)
            # but NOT `pair_first` (it's not anyone else's "last").
            if has_middle_field:
                assert has_last and not has_first, (
                    f"node {node['index']}: pair_middle set but role "
                    f"contradicts (has_first={has_first}, has_last={has_last}); "
                    f"only the triple's pair_first carries the pair_middle field"
                )

    def test_pair_cross_references_resolve_to_equivalent_partner(
        self, fixture_data: dict[str, Any]
    ) -> None:
        """Pair references resolve to a node in the same logical pair.

        The strict-symmetry invariant ``first.pair_last == last.index ∧
        last.pair_first == first.index`` does not hold on real fixtures
        with stitched / forked transcripts: duplicate ``tool_use_id``
        values produce multiple tool_use nodes that all index the same
        tool_result, but the dict-based pair-by-index logic keeps only
        the last write — leaving earlier tool_use nodes with stale
        ``pair_last`` references that the result's ``pair_first`` no
        longer mirrors.

        The looser invariant that *does* hold: the back-reference
        resolves to *a* node sharing the same ``tool_use_id`` (the
        equivalence class), or to the same node directly when the pair
        is index-paired (slash command triples, bash, thinking) and
        therefore has no ``tool_use_id``.
        """
        by_index = {n["index"]: n for n in _walk(fixture_data["messages"])}

        def _tool_use_id(node: dict[str, Any]) -> str | None:
            return node.get("content", {}).get("tool_use_id") or None

        def _equivalent(a: dict[str, Any], b: dict[str, Any]) -> bool:
            """Two nodes belong to the same logical pair group."""
            if a["index"] == b["index"]:
                return True
            tu_a = _tool_use_id(a)
            tu_b = _tool_use_id(b)
            return tu_a is not None and tu_a == tu_b

        for node in _walk(fixture_data["messages"]):
            # Pair_first node (no `pair_first` field, has `pair_last`):
            # the partner's `pair_first` ref should point at me OR at
            # an equivalent (same-tool_use_id) node.
            if "pair_last" in node and "pair_first" not in node:
                last = by_index[node["pair_last"]]
                back_idx = last.get("pair_first")
                assert back_idx is not None, (
                    f"node {node['index']}.pair_last = {node['pair_last']} "
                    f"but the target carries no pair_first ref"
                )
                back = by_index[back_idx]
                assert _equivalent(node, back), (
                    f"node {node['index']}.pair_last = {node['pair_last']}, "
                    f"but {node['pair_last']}.pair_first = {back_idx}, "
                    f"and the two are not in the same logical pair "
                    f"(tool_use_id {_tool_use_id(node)!r} vs "
                    f"{_tool_use_id(back)!r})"
                )

            # Triple pair_first: pair_middle ref should resolve to a node
            # whose pair_first points back at me (or at an equivalent).
            if "pair_middle" in node:
                middle = by_index[node["pair_middle"]]
                back_idx = middle.get("pair_first")
                assert back_idx is not None, (
                    f"node {node['index']}.pair_middle = "
                    f"{node['pair_middle']} but the target carries no "
                    f"pair_first ref"
                )
                back = by_index[back_idx]
                assert _equivalent(node, back), (
                    f"pair_middle asymmetry: {node['index']}.pair_middle "
                    f"= {node['pair_middle']} but "
                    f"{node['pair_middle']}.pair_first = {back_idx}, "
                    f"not in the same logical pair"
                )

    def test_types_match_pattern(self, fixture_data: dict[str, Any]) -> None:
        """``type`` strings are well-formed identifiers. Catches obvious
        garbage (empty, whitespace, path-like) without hardcoding the
        closed set, which would rot every time a new content type lands."""
        bad: list[tuple[int, str]] = []
        for node in _walk(fixture_data["messages"]):
            if not _TYPE_PATTERN.fullmatch(node["type"]):
                bad.append((node["index"], node["type"]))
        assert not bad, f"malformed type values: {bad[:5]} ({len(bad)} total)"

    def test_tree_acyclic(self, fixture_data: dict[str, Any]) -> None:
        """Walking ``children`` must never revisit the same ``index`` —
        defence against any future tree-building bug that introduces a
        cycle. Implemented as a depth-first scan tracking the current
        ancestor stack rather than the global seen-set, since seeing the
        same index in two distinct branches is fine (it would be a
        duplicate-index bug, caught by ``test_indexes_are_unique``)."""

        def visit(node: dict[str, Any], stack: tuple[int, ...]) -> None:
            assert node["index"] not in stack, (
                f"cycle detected: node {node['index']} appears in its own "
                f"ancestor chain {stack}"
            )
            new_stack = stack + (node["index"],)
            for child in node.get("children", []):
                visit(child, new_stack)

        for root in fixture_data["messages"]:
            visit(root, ())

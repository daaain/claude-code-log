"""Nested sub-agent hierarchies (issue #213).

Claude Code 2.1.172+ lets sub-agents spawn their own sub-agents. All
transcripts land FLAT in the trunk session's ``subagents/`` dir; the only
depth-proof links are the in-band ``agentId:`` result tail and the sidecar
``agent-<id>.meta.json``'s ``toolUseId`` (which also covers interrupted
spawns that never produced a usable tool_result). These tests pin the
loader's sidecar-driven discovery (``spawnedAgentId``), the DAG parentage,
the depth-shifted hierarchy levels, and the recursive block relocation —
each agent's transcript must nest under its own spawn pair at any depth.

Fixture: ``test/test_data/nested_agents/`` (see
``scripts/gen_nested_agents_fixture.py``) — a 2×2 fan-out, a 3-deep chain,
and one interrupted spawn with a meta-only link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from claude_code_log.converter import (
    _integrate_agent_entries,
    load_directory_transcripts,
    load_transcript,
)
from claude_code_log.dag import build_dag_from_entries
from claude_code_log.models import BaseTranscriptEntry, TranscriptEntry
from claude_code_log.renderer import TemplateMessage, generate_template_messages

TRUNK_SID = "33330000-0000-4000-8000-000000000001"
TRUNK = Path(__file__).parent / "test_data" / "nested_agents" / f"{TRUNK_SID}.jsonl"

MID1, MID2 = "nsmid001", "nsmid002"
LEAVES = ["nsleaf11", "nsleaf12", "nsleaf21", "nsleaf22"]
CHAIN1, CHAIN2, CHAIN3 = "nschain1", "nschain2", "nschain3"
INTR = "nsintr01"
ALL_AGENTS = [MID1, MID2, *LEAVES, CHAIN1, CHAIN2, CHAIN3, INTR]

# Spawner → spawned (the ground truth the linkage must reproduce).
SPAWNED_BY = {
    MID1: None,  # trunk
    MID2: None,
    CHAIN1: None,
    INTR: None,
    "nsleaf11": MID1,
    "nsleaf12": MID1,
    "nsleaf21": MID2,
    "nsleaf22": MID2,
    CHAIN2: CHAIN1,
    CHAIN3: CHAIN2,
}


def _load_integrated() -> list[TranscriptEntry]:
    entries = load_transcript(TRUNK, silent=True)
    _integrate_agent_entries(entries)
    return entries


def _line_of(sid: str) -> Optional[str]:
    return sid.rsplit("#agent-", 1)[-1] if "#agent-" in sid else None


def _base_entries(entries: list[TranscriptEntry]) -> list[BaseTranscriptEntry]:
    """The entries carrying DAG/agent fields (drops Summary & friends)."""
    return [e for e in entries if isinstance(e, BaseTranscriptEntry)]


def _members(entries: list[TranscriptEntry]) -> set[str]:
    """Membership ids — whose transcripts actually loaded. (The trunk's
    own spawn anchors carry the legacy agentId backpatch, so restrict to
    sidechain entries.)"""
    return {e.agentId for e in _base_entries(entries) if e.isSidechain and e.agentId}


class TestNestedDiscovery:
    def test_all_transcripts_load_at_every_depth(self) -> None:
        entries = load_transcript(TRUNK, silent=True)
        assert _members(entries) == set(ALL_AGENTS)

    def test_spawn_links_resolved_from_sidecars(self) -> None:
        entries = load_transcript(TRUNK, silent=True)
        links: dict[str, Optional[str]] = {}
        for e in _base_entries(entries):
            if e.spawnedAgentId:
                # The spawning entry's membership is the spawner.
                links[e.spawnedAgentId] = e.agentId if e.isSidechain else None
        # Trunk anchors got the legacy agentId backpatch too — their
        # membership reads as the spawned id itself; normalize.
        for child, parent in SPAWNED_BY.items():
            if parent is None:
                assert child in links, f"{child} not linked"
                assert links[child] in (None, child)
            else:
                assert links.get(child) == parent

    def test_interrupted_spawn_links_via_meta_only(self) -> None:
        # The rejected spawn's tool_result has no tail and no toolUseResult:
        # the sidecar's toolUseId must still link the transcript.
        entries = load_transcript(TRUNK, silent=True)
        intr_entries = [
            e for e in _base_entries(entries) if e.isSidechain and e.agentId == INTR
        ]
        assert intr_entries, "interrupted agent's transcript must load"
        anchors = [e for e in _base_entries(entries) if e.spawnedAgentId == INTR]
        assert len(anchors) == 1
        assert not anchors[0].isSidechain

    def test_directory_load_matches_single_file(self) -> None:
        msgs, _tree = load_directory_transcripts(TRUNK.parent, silent=True)
        assert _members(msgs) == set(ALL_AGENTS)


class TestNestedDag:
    def test_depth_histogram(self) -> None:
        tree = build_dag_from_entries(_load_integrated())

        def depth(sid: str, seen: tuple[str, ...] = ()) -> int:
            line = tree.sessions.get(sid)
            if line is None or line.parent_session_id is None or sid in seen:
                return 0
            return 1 + depth(line.parent_session_id, seen + (sid,))

        histogram: dict[int, int] = {}
        for sid in tree.sessions:
            histogram[depth(sid)] = histogram.get(depth(sid), 0) + 1
        assert histogram == {0: 1, 1: 4, 2: 5, 3: 1}

    def test_no_unparented_sidechain_roots(self) -> None:
        orphans = [
            e
            for e in _base_entries(_load_integrated())
            if e.isSidechain and e.parentUuid is None
        ]
        assert orphans == []


class TestNestedTree:
    def _tree(self) -> tuple[list[TemplateMessage], dict[str, list[str]]]:
        """Build the template tree + a map of agent line → ancestor lines
        (the agent-line sequence above each agent's topmost tree nodes)."""
        roots, _nav, ctx = generate_template_messages(_load_integrated())
        by_index = {m.message_index: m for m in ctx.messages if m is not None}
        lines_above: dict[str, list[str]] = {}

        def visit(node: TemplateMessage) -> None:
            line = _line_of(node.meta.session_id or "")
            if line and line not in lines_above:
                # Ancestors of the line's OWN transcript don't count (the
                # ancestry may retain its later-deduped prompt entry); the
                # chain is the SPAWNER path above the transcript.
                chain: list[str] = []
                for idx in node.ancestry:
                    anc = by_index.get(idx)
                    if anc is None:
                        continue
                    anc_line = _line_of(anc.meta.session_id or "")
                    if (
                        anc_line
                        and anc_line != line
                        and (not chain or chain[-1] != anc_line)
                    ):
                        chain.append(anc_line)
                lines_above[line] = chain
            for child in node.children:
                visit(child)

        for root in roots:
            visit(root)
        return roots, lines_above

    def test_each_agent_nests_under_its_spawner(self) -> None:
        # Transcripts whose every entry duplicates its spawn pair (verbatim
        # leaves, the chain bottom) collapse entirely — exactly like the
        # depth-1 dedup; the others must hang under their true spawner.
        _roots, lines_above = self._tree()
        assert set(lines_above) == {MID1, MID2, "nsleaf22", CHAIN1, CHAIN2, INTR}
        for child, chain in lines_above.items():
            parent = SPAWNED_BY[child]
            if parent is None:
                assert chain == [], f"{child} must hang off the trunk, got {chain}"
            else:
                assert chain and chain[-1] == parent, (
                    f"{child} must nest inside {parent}, got {chain}"
                )

    def test_chain_ancestry_is_the_full_path(self) -> None:
        # chain2's spawn pair (the deepest surviving chain nodes) sits
        # inside chain1's transcript; chain3's own answer collapses into
        # chain2's tool_result content (verbatim duplicate).
        _roots, lines_above = self._tree()
        assert lines_above.get(CHAIN2) == [CHAIN1]
        assert CHAIN3 not in lines_above

    def test_divergent_leaf_survives_dedup_at_depth_2(self) -> None:
        # leaf22's answer differs from the spawn result (truncated copy), so
        # its transcript stays visible — nested inside mid2.
        _roots, lines_above = self._tree()
        assert lines_above.get("nsleaf22") == [MID2]
        # Its verbatim siblings collapse entirely (prompt + answer are
        # duplicates of the spawn pair) — same as depth-1 behavior.
        for collapsed in ("nsleaf11", "nsleaf12", "nsleaf21"):
            assert collapsed not in lines_above

    def test_interrupted_transcript_nests_under_error_result(self) -> None:
        _roots, lines_above = self._tree()
        assert lines_above.get(INTR) == []


class TestNestedVisualLayer:
    """The #213 visual layer: per-message agent_depth, the fully-collapsed
    marker, and the spawn-card depth badge."""

    def _ctx_messages(self) -> list[TemplateMessage]:
        _roots, _nav, ctx = generate_template_messages(_load_integrated())
        return [m for m in ctx.messages if m is not None]

    def test_agent_depth_set_per_session_line(self) -> None:
        msgs = self._ctx_messages()
        # Highest depth among messages that survived rendering. chain3 (d3)
        # and the leaves (d2) mostly collapse; mid/chain1 content is d1, and
        # chain2's surviving spawn pair is d2.
        by_line_depth = {
            _line_of(m.meta.session_id or ""): m.agent_depth
            for m in msgs
            if _line_of(m.meta.session_id or "")
        }
        assert by_line_depth.get(MID1) == 1
        assert by_line_depth.get(MID2) == 1
        assert by_line_depth.get(CHAIN1) == 1
        assert by_line_depth.get(CHAIN2) == 2
        assert by_line_depth.get("nsleaf22") == 2
        # Trunk messages stay at depth 0.
        assert all(
            m.agent_depth == 0 for m in msgs if not _line_of(m.meta.session_id or "")
        )

    def test_collapsed_flag_marks_verbatim_nested_spawns_only(self) -> None:
        msgs = self._ctx_messages()
        collapsed = {
            m.meta.spawned_agent_id for m in msgs if m.spawns_collapsed_transcript
        }
        # Three verbatim leaves + the chain bottom collapse; the divergent
        # leaf22 and the interrupted spawn do not.
        assert collapsed == {"nsleaf11", "nsleaf12", "nsleaf21", CHAIN3}

    def test_collapsed_flag_never_on_trunk_level_spawns(self) -> None:
        # Trunk-level (depth-1-spawning) Task/Agent results keep their
        # pre-#213 rendering — the marker is nested-only.
        msgs = self._ctx_messages()
        assert all(m.agent_depth >= 1 for m in msgs if m.spawns_collapsed_transcript)

    def test_depth_badge_html_uses_spawned_depth(self) -> None:
        from claude_code_log.html.renderer import generate_html

        html = generate_html(_load_integrated(), "badge")
        # A leaf-spawn card (inside a depth-1 agent) opens depth 2.
        assert "Depth 2</span>" in html
        # chain2's spawn of chain3 opens depth 3.
        assert "Depth 3</span>" in html
        # The collapsed marker renders.
        assert "≡ full transcript" in html

    def test_new_sidecar_invalidates_cached_trunk(self, tmp_path: Path) -> None:
        """Sidecar inputs are part of the cache key (PR #218 review).

        The trunk jsonl's mtime alone can't see a sidecar that appears
        AFTER the transcript was cached (nested spawns never touch the
        trunk file): without the subagents fingerprint the cached —
        agent-less — parse would be served forever."""
        import shutil

        from claude_code_log.cache import CacheManager

        proj = tmp_path / "proj"
        proj.mkdir()
        trunk = proj / TRUNK.name
        shutil.copy(TRUNK, trunk)

        cm = CacheManager(proj, "0.0.0-test", db_path=tmp_path / "cache.db")
        first = load_transcript(trunk, cache_manager=cm, silent=True)
        assert _members(first) == set(), "no agent transcripts on disk yet"
        # Baseline cache hit while the world is unchanged.
        assert cm.is_file_cached(trunk)

        # The agents finish: transcripts + sidecars appear, trunk untouched.
        shutil.copytree(TRUNK.parent / TRUNK_SID, proj / TRUNK_SID)

        assert not cm.is_file_cached(trunk), (
            "new sidecars must invalidate the cached parse"
        )
        rediscovered = load_transcript(trunk, cache_manager=cm, silent=True)
        assert _members(rediscovered) == set(ALL_AGENTS)
        # And the refreshed cache entry is valid again.
        assert cm.is_file_cached(trunk)

    def test_sidecar_landing_mid_parse_invalidates_next_read(
        self, tmp_path: Path
    ) -> None:
        """TOCTOU window (delta-review advisory): the stored fingerprint
        must describe the world AS OF THE PARSE — a sidecar landing
        between the parse's sidecar scan and the save must mismatch on
        the next read (over-invalidation), not be fingerprinted as
        covered by a parse that never saw it."""
        import shutil

        from claude_code_log.cache import CacheManager, subagents_fingerprint

        proj = tmp_path / "proj"
        proj.mkdir()
        trunk = proj / TRUNK.name
        shutil.copy(TRUNK, trunk)

        cm = CacheManager(proj, "0.0.0-test", db_path=tmp_path / "cache.db")
        # The parse captured its fingerprint…
        fp_at_parse = subagents_fingerprint(trunk)
        entries = load_transcript(trunk, silent=True)
        # …then a sidecar landed before the save.
        shutil.copytree(TRUNK.parent / TRUNK_SID, proj / TRUNK_SID)
        cm.save_cached_entries(trunk, entries, subagents_fp=fp_at_parse)

        assert not cm.is_file_cached(trunk), (
            "a parse-time fingerprint must not validate the late sidecar"
        )


class TestMultiSpawnGuard:
    def test_resultless_parallel_spawns_in_one_entry_degrade_safely(self) -> None:
        """Degenerate shape (unobserved in real transcripts — Claude Code
        streams one content block per assistant entry): a single entry
        with TWO resultless spawn tool_uses. The single spawnedAgentId
        keeps the first link, never silently overwrites, and both
        transcripts still join the loading set."""
        from claude_code_log.converter import _apply_subagent_meta_links
        from claude_code_log.factories import create_transcript_entry

        entry = create_transcript_entry(
            {
                "type": "assistant",
                "uuid": "ms-a1",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/repo",
                "sessionId": "ms-trunk",
                "version": "2.1.173",
                "timestamp": "2026-06-12T09:00:00.000Z",
                "message": {
                    "id": "msg_ms-a1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5-20251001",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_ms_1",
                            "name": "Agent",
                            "input": {"prompt": "one"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_ms_2",
                            "name": "Agent",
                            "input": {"prompt": "two"},
                        },
                    ],
                },
            }
        )
        assert isinstance(entry, BaseTranscriptEntry)
        agent_ids: set[str] = set()
        _apply_subagent_meta_links(
            [entry],
            {"toolu_ms_1": "agms0001", "toolu_ms_2": "agms0002"},
            agent_ids,
            Path("ms-trunk.jsonl"),
        )
        assert agent_ids == {"agms0001", "agms0002"}, "both transcripts load"
        # Deterministic first link kept (sorted iteration), second skipped.
        assert entry.spawnedAgentId == "agms0001"


class TestNestedRendering:
    def test_html_renders_nested_content(self) -> None:
        from claude_code_log.html.renderer import generate_html

        html = generate_html(_load_integrated(), "Nested Agents Test")
        # The chain bottom's answer surfaces in chain2's tool_result.
        assert "depth 3: BOTTOM" in html
        # The surviving depth-2 leaf's full (untruncated) answer — matched
        # without the asterisks, which Markdown turns into <em> markup.
        assert "(10-2) = 90 - 18" in html
        # The interrupted agent's transcript renders despite the rejected
        # tool_result.
        assert "Looping…" in html

    def test_markdown_renders_nested_content(self) -> None:
        from claude_code_log.markdown.renderer import MarkdownRenderer

        md = MarkdownRenderer().generate(_load_integrated())
        assert "depth 3: BOTTOM" in md
        assert "(10-2) = 90 - 18" in md

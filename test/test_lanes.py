"""The branch-lane model (``claude_code_log/lanes.py``, minimal theme P5).

``annotate_lanes`` assigns every render-tree node to a lane — ``main``, a
sub-agent (``agent-<id>``), a workflow agent (``wfagent-<id>``) or a rewind
fork (``branch-<sid>``) — and
describes each lane (spawn and merge rows, user turn, rank, stats). The
minimal theme emits it as ``data-*`` attributes for the DAG engine
(work/minimal-theme-dag.md, P5 "As built"); classic output never sees it.

Fixtures: ``async_agents/``, ``nested_agents/``, ``teammates/``,
``workflow_basic/`` (loaded with ``load_directory_transcripts`` — the
single-file loader skips agent integration), ``dag_within_fork.jsonl`` and
``dag_compact_after_rewind.jsonl``, plus small synthetic transcripts for
the shapes no fixture has (several forks in one turn, a fork inside a fork,
an agent inside a fork, a visible three-deep agent chain, a rewind inside an
agent, a fork point filtered to a landmark).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

import pytest

from claude_code_log.converter import (
    convert_jsonl_to,
    load_directory_transcripts,
    load_transcript,
)
from claude_code_log.html.minimal_theme import lane_attributes
from claude_code_log.html.renderer import HtmlRenderer, generate_html
from claude_code_log.lanes import MAIN_LANE, LaneModel, annotate_lanes, teammate_links
from claude_code_log.models import RenderingDepth
from claude_code_log.renderer import TemplateMessage, generate_template_messages
from test.dag_demo_fixture import write_team_demo, write_workflow_demo

TEST_DATA = Path(__file__).parent / "test_data"
ASYNC = TEST_DATA / "async_agents"
NESTED = TEST_DATA / "nested_agents"
TEAMMATES = TEST_DATA / "teammates"
WORKFLOW = TEST_DATA / "workflow_basic"


# ---------------------------------------------------------------- helpers


def _model(
    path: Path, depth: RenderingDepth = RenderingDepth.HOOK
) -> tuple[LaneModel, dict[int, TemplateMessage]]:
    """Lane model + message_index → node for a fixture file or directory."""
    if path.is_dir():
        entries, tree = load_directory_transcripts(path, silent=True)
    else:
        entries, tree = load_transcript(path, silent=True), None
    roots, _nav, _ctx = generate_template_messages(
        entries, session_tree=tree, depth=depth
    )
    model = annotate_lanes(roots)
    nodes: dict[int, TemplateMessage] = {}
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.message_index is not None:
            nodes[node.message_index] = node
        stack.extend(node.children)
    return model, nodes


def _minimal_html(path: Path, depth: RenderingDepth = RenderingDepth.HOOK) -> str:
    if path.is_dir():
        entries, tree = load_directory_transcripts(path, silent=True)
    else:
        entries, tree = load_transcript(path, silent=True), None
    renderer = HtmlRenderer()
    renderer.theme = "minimal"
    renderer.depth = depth
    return renderer.generate(entries, "Lanes", session_tree=tree)


_CARD = re.compile(r"<div class='(?:message|fork-point)[^>]*\sid='msg-(d-\d+)'([^>]*)>")
_ATTR = re.compile(r'\s(data-[\w-]+)="([^"]*)"')


def _card_attrs(html: str) -> dict[str, dict[str, str]]:
    """``d-N`` → the card's ``data-*`` attributes (double-quoted ones)."""
    return {card_id: dict(_ATTR.findall(rest)) for card_id, rest in _CARD.findall(html)}


TS0 = "2026-07-01T10:00:00.000Z"


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-07-01T10:{minute:02d}:{second:02d}.000Z"


def _entry(
    kind: str,
    uuid: str,
    parent: Optional[str],
    ts: str,
    content: Any,
    *,
    sid: str = "s1",
    agent: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": agent is not None,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": sid,
        "version": "2.1.180",
        "timestamp": ts,
    }
    if agent is not None:
        entry["agentId"] = agent
    if kind == "user":
        entry["message"] = {"role": "user", "content": content}
    else:
        entry["message"] = {
            "id": f"msg_{uuid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "stop_reason": "end_turn",
            "content": content,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
    entry.update(extra)
    return entry


def _say(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def _spawn(tool_id: str, description: str, **inp: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_use",
            "id": tool_id,
            "name": "Agent",
            "input": {
                "description": description,
                "subagent_type": "general-purpose",
                "prompt": f"Do: {description}",
                **inp,
            },
        }
    ]


def _result(tool_id: str, text: str, agent_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "text",
                    "text": f"agentId: {agent_id} (use SendMessage with to: "
                    f"'{agent_id}' to continue this agent)\n<usage>total_tokens: "
                    "48400\ntool_uses: 3\nduration_ms: 133000</usage>",
                },
            ],
        }
    ]


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _agent_file(
    session_dir: Path,
    agent_id: str,
    tool_use_id: str,
    description: str,
    entries: list[dict[str, Any]],
) -> None:
    sub = session_dir / "subagents"
    _write(sub / f"agent-{agent_id}.jsonl", entries)
    (sub / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": description,
                "toolUseId": tool_use_id,
            }
        ),
        encoding="utf-8",
    )


def _branch_lanes(model: LaneModel) -> dict[str, Any]:
    return {lane.lane_id: lane for lane in model.branches}


# ---------------------------------------------------------------- fixtures


class TestAsyncAgents:
    def test_lane(self) -> None:
        model, nodes = _model(ASYNC)
        assert list(model.lanes) == ["agent-cccc333"]
        lane = model.lanes["agent-cccc333"]
        assert lane.kind == "async-agent"
        assert lane.name == "Coverage analysis"
        assert lane.parent_lane == MAIN_LANE and lane.depth == 1
        # Spawn = the Task tool_use; merge = the <task-notification>, not the
        # launch stub (that is the spawn's pair partner, still on main).
        spawn = nodes[lane.spawn_index or -1]
        assert spawn.type == "tool_use" and spawn.lane_id == MAIN_LANE
        merge = nodes[lane.merge_index or -1]
        assert merge.type == "task_notification" and merge.lane_id == MAIN_LANE
        stub = nodes[spawn.pair_last or -1]
        assert stub.type == "tool_result" and stub.message_index != lane.merge_index
        assert lane.head_index == lane.spawn_index
        # Accounting from the notification's <usage> block.
        assert lane.stats == "1 step · 23.1k tokens · 15.5s"
        assert lane.meta == "Explore · claude-opus-4-7 · async"
        assert lane.tag == "coverage"
        turn = nodes[lane.turn_index or -1]
        assert turn.type == "user" and lane.rank == 1
        assert lane.first_ts == lane.last_ts == "2026-04-19T10:04:00.000Z"

    def test_card_lanes(self) -> None:
        model, nodes = _model(ASYNC)
        lane = model.lanes["agent-cccc333"]
        members = [n for n in nodes.values() if n.lane_id == lane.lane_id]
        assert members and all(n.is_sidechain for n in members)
        assert lane.first_index == min(n.message_index or 0 for n in members)
        assert all(n.lane_id == MAIN_LANE for n in nodes.values() if not n.is_sidechain)


class TestNestedAgents:
    # nsleaf11/12/21 and nschain3 answer verbatim through their spawn pair,
    # so their transcripts dedupe away (#213) and they get no lane.
    EXPECTED_PARENTS = {
        "agent-nsmid001": MAIN_LANE,
        "agent-nsmid002": MAIN_LANE,
        "agent-nsleaf22": "agent-nsmid002",
        "agent-nschain1": MAIN_LANE,
        "agent-nschain2": "agent-nschain1",
        "agent-nsintr01": MAIN_LANE,
    }

    def test_lanes_and_parents(self) -> None:
        model, _nodes = _model(NESTED)
        lanes = _branch_lanes(model)
        assert list(lanes) == list(self.EXPECTED_PARENTS)
        assert {k: v.parent_lane for k, v in lanes.items()} == self.EXPECTED_PARENTS
        assert {k: v.depth for k, v in lanes.items()} == {
            "agent-nsmid001": 1,
            "agent-nsmid002": 1,
            "agent-nsleaf22": 2,
            "agent-nschain1": 1,
            "agent-nschain2": 2,
            "agent-nsintr01": 1,
        }
        assert all(lane.kind == "agent" for lane in lanes.values())

    def test_spawn_and_merge_sit_in_the_parent_lane(self) -> None:
        model, nodes = _model(NESTED)
        for lane in model.branches:
            spawn = nodes[lane.spawn_index or -1]
            merge = nodes[lane.merge_index or -1]
            assert spawn.type == "tool_use" and merge.type == "tool_result"
            assert spawn.pair_last == merge.message_index
            assert spawn.lane_id == merge.lane_id == lane.parent_lane

    def test_collapsed_chain_end_stays_in_its_parent(self) -> None:
        """nschain3's spawn pair is a card of nschain2 — depth 3 on the
        hierarchy — but its own transcript collapsed, so it is no lane."""
        model, nodes = _model(NESTED)
        assert "agent-nschain3" not in model.lanes
        spawn_of_3 = [
            n
            for n in nodes.values()
            if n.type == "tool_use" and n.lane_id == "agent-nschain2"
        ]
        assert len(spawn_of_3) == 1 and spawn_of_3[0].agent_depth == 2

    def test_interrupted_spawn_merges_at_its_error_stub(self) -> None:
        model, nodes = _model(NESTED)
        lane = model.lanes["agent-nsintr01"]
        assert nodes[lane.merge_index or -1].content.is_error  # type: ignore[union-attr]
        assert lane.stats == "1 step"

    def test_stats_from_the_result_tail(self) -> None:
        model, _ = _model(NESTED)
        # Two cards are pairs' second halves: 4 cards, 2 steps.
        mid1 = model.lanes["agent-nsmid001"]
        assert (mid1.cards, mid1.steps) == (4, 2)
        assert mid1.stats == "2 steps · 1.5s"  # duration_ms 1500, no total_tokens

    def test_one_turn_ranks_by_spawn_order(self) -> None:
        model, _ = _model(NESTED)
        lanes = model.branches
        assert len({lane.turn_index for lane in lanes}) == 1
        assert [lane.rank for lane in lanes] == [1, 2, 3, 4, 5, 6]


class TestTeammates:
    def test_threads_are_not_branches(self) -> None:
        model, nodes = _model(TEAMMATES)
        assert model.branches == []
        mates = {lane.name: lane for lane in model.teammates}
        assert set(mates) == {"alice", "bob"}
        for lane in mates.values():
            assert lane.kind == "teammate" and lane.rank == 0
            first = nodes[lane.first_index or -1]
            assert first.is_sidechain and first.lane_id == MAIN_LANE
            assert nodes[lane.spawn_index or -1].type == "tool_use"
        # Every node of the page stays on the main lane.
        assert {n.lane_id for n in nodes.values()} == {MAIN_LANE}

    def test_spawn_card_links_to_the_thread(self) -> None:
        attrs = _card_attrs(_minimal_html(TEAMMATES))
        links = {
            a["data-teammate-name"]: (card, a["data-teammate-link"])
            for card, a in attrs.items()
            if "data-teammate-link" in a
        }
        assert set(links) == {"alice", "bob"}
        for _card, target in links.values():
            assert attrs[target]["data-lane"] == "main"
        assert not any("data-lane-id" in a for a in attrs.values())


class TestWorkflow:
    """Workflow agents (#174) as lanes: one per agent card whose side-channel
    transcript rendered, spawned at its phase card, merging at the agent
    card, ranked and capped per phase (``data-lane-group``)."""

    def test_each_agent_with_a_transcript_is_a_lane(self) -> None:
        model, nodes = _model(WORKFLOW)
        lanes = _branch_lanes(model)
        assert list(lanes) == [
            "wfagent-ag000001",
            "wfagent-ag000002",
            "wfagent-ag000003",
        ]
        for lane_id, lane in lanes.items():
            spawn = nodes[lane.spawn_index]
            head = nodes[lane.head_index]
            assert lane.kind == "workflow-agent" and lane.parent_lane == MAIN_LANE
            assert spawn.type == "workflow_phase" and spawn.lane_id == MAIN_LANE
            assert head.type == "workflow_agent" and head.lane_id == MAIN_LANE
            assert lane.merge_index == lane.head_index and lane.state == ""
            # The agent card's transcript is the lane, all of it.
            stack = list(head.children)
            assert stack
            while stack:
                node = stack.pop()
                assert node.in_workflow_sidechannel and node.lane_id == lane_id
                stack.extend(node.children)
        loader = lanes["wfagent-ag000001"]
        assert (loader.name, loader.tag) == ("review:loader", "loader")
        assert loader.meta == "Map · claude-sonnet-4-6"
        assert loader.stats == "4 steps · 100 tokens · 1.0s"
        # Phases are groups: ranks restart in each, the turn is shared.
        assert [(lane.group, lane.rank) for lane in lanes.values()] == [
            ("wf_demo01/0", 1),
            ("wf_demo01/0", 2),
            ("wf_demo01/1", 1),
        ]
        assert {lane.turn_index for lane in lanes.values()} == {1}
        assert (
            lanes["wfagent-ag000001"].spawn_index
            == lanes["wfagent-ag000002"].spawn_index
        )

    def test_demo_groups_rows_and_states(self, tmp_path: Path) -> None:
        model, nodes = _model(write_workflow_demo(tmp_path / "wf"))
        lanes = _branch_lanes(model)
        map_lanes = [lane for lane in lanes.values() if lane.group == "wf_theme01/0"]
        assert [lane.name for lane in map_lanes] == [
            "map:loader",
            "map:renderer",
            "map:lanes",
            "map:engine",
            "map:styles",
        ]
        # Ranked by when each agent started.
        assert [lane.rank for lane in map_lanes] == [1, 2, 3, 4, 5]
        # The agent with no transcript is a plain row, not a lane.
        docs = [
            n
            for n in nodes.values()
            if n.type == "workflow_agent" and "docs" in str(getattr(n.content, "label"))
        ]
        assert len(docs) == 1 and docs[0].lane_id == MAIN_LANE
        assert not any("docs" in lane.lane_id for lane in lanes.values())
        # A failed agent without a result: no merge row, ended.
        engine = lanes["wfagent-wa04engine"]
        assert engine.merge_index is None and engine.state == "ended"
        assert engine.meta == "Map · claude-haiku-4-5 · failed"
        # The turn's own sub-agent ranks first in the turn, unaffected.
        task = lanes["agent-c900check"]
        assert task.kind == "agent" and task.rank == 1 and task.group == ""
        assert task.turn_index == map_lanes[0].turn_index

    def test_a_run_without_phases_spawns_at_the_call_and_stays_open(self) -> None:
        """A run with no snapshot hangs its agents off the Workflow call's
        result; an agent without a result there may still be running."""
        entries, tree = load_directory_transcripts(WORKFLOW, silent=True)
        assert tree is not None
        for run in tree.workflow_runs.values():
            run.has_snapshot = False
            for agent in run.agents:
                if agent.agent_id == "ag000003":
                    agent.result, agent.result_preview, agent.state = None, "", ""
        roots, _nav, _ctx = generate_template_messages(
            entries, session_tree=tree, depth=RenderingDepth.HOOK
        )
        model = annotate_lanes(roots)
        lanes = _branch_lanes(model)
        assert len(lanes) == 3
        spawns = {lane.spawn_index for lane in lanes.values()}
        assert len(spawns) == 1
        assert {lane.group for lane in lanes.values()} == {"wf_demo01"}
        open_lane = lanes["wfagent-ag000003"]
        assert open_lane.merge_index is None and open_lane.state == "open"
        attrs = lane_attributes(model)
        spawn_index = spawns.pop()
        assert spawn_index is not None
        spawn = dict(attrs[spawn_index])
        assert spawn["data-spawns"].split() == list(lanes)

    def test_html_attributes(self) -> None:
        attrs = _card_attrs(_minimal_html(WORKFLOW))
        assert attrs["d-5"]["data-spawns"] == "wfagent-ag000001 wfagent-ag000002"
        head = attrs["d-6"]
        assert head == {
            "data-lane": "main",
            "data-merges": "wfagent-ag000001",
            "data-lane-id": "wfagent-ag000001",
            "data-lane-kind": "workflow-agent",
            "data-lane-name": "review:loader",
            "data-lane-tag": "loader",
            "data-lane-meta": "Map · claude-sonnet-4-6",
            "data-lane-parent": "main",
            "data-lane-depth": "1",
            "data-lane-from": "d-5",
            "data-lane-to": "d-6",
            "data-lane-turn": "d-1",
            "data-lane-group": "wf_demo01/0",
            "data-lane-rank": "1",
            "data-lane-stats": "4 steps · 100 tokens · 1.0s",
            "data-lane-ts": "2026-06-04T10:00:00.000Z 2026-06-04T10:00:00.000Z",
        }
        assert attrs["d-7"] == {"data-lane": "wfagent-ag000001"}

    def test_classic_carries_no_lane_data(self) -> None:
        entries, tree = load_directory_transcripts(WORKFLOW, silent=True)
        classic = generate_html(entries, "T", session_tree=tree)
        assert "data-lane" not in classic and "wfagent-" not in classic


class TestForkFixtures:
    def test_within_fork(self) -> None:
        model, nodes = _model(TEST_DATA / "dag_within_fork.jsonl")
        assert list(model.lanes) == ["branch-s1@d_prime"]
        lane = model.lanes["branch-s1@d_prime"]
        assert lane.kind == "fork" and lane.merge_index is None
        assert lane.parent_lane == MAIN_LANE and lane.depth == 1
        fork_point = nodes[lane.spawn_index or -1]
        assert fork_point.junction_forward_links
        assert nodes[lane.head_index or -1].is_branch_header
        assert lane.name == "Branch 2 start" and lane.tag == "fork"
        # The fork point is itself a user turn, so it is the group.
        assert lane.turn_index == fork_point.message_index and lane.rank == 1
        assert lane.stats == "2 steps · 1m 0s"
        # The earliest branch continues main (§ 7 decision 3).
        (header, (continued, fp)), *_ = model.continuations.items()
        assert nodes[header].is_branch_header and continued == MAIN_LANE
        assert fp == fork_point.message_index
        cont_cards = [
            n
            for n in nodes.values()
            if n.render_session_id == "s1@d" and not n.is_session_header
        ]
        assert cont_cards and {n.lane_id for n in cont_cards} == {MAIN_LANE}

    def test_compaction_after_rewind_stays_in_the_fork(self) -> None:
        """#331: a /compact on a rewind branch continues that branch, so the
        boundary and everything after it belong to the fork lane."""
        model, nodes = _model(TEST_DATA / "dag_compact_after_rewind.jsonl")
        lane = model.lanes["branch-s1@b2u"]
        members = [n for n in nodes.values() if n.lane_id == lane.lane_id]
        types = {n.type for n in members}
        assert "system" in types  # the compact_boundary
        assert any(
            type(n.content).__name__ == "CompactedSummaryMessage" for n in members
        )
        assert lane.last_ts == "2025-07-01T10:41:00.000Z"
        assert lane.steps == len(members) - 1  # minus the branch header


# ---------------------------------------------------------------- synthetic


def _fork_tree(tmp_path: Path) -> Path:
    """s1: one turn whose answer is rewound five times; the second branch is
    rewound again inside itself."""
    e = [
        _entry("user", "u1", None, _ts(0), _say("Start")),
        _entry("assistant", "a1", "u1", _ts(1), _say("Ready")),
    ]
    # Five rewinds of a1, at distinct times (u2 earliest → continues main).
    for i, minute in enumerate((2, 10, 30, 40, 50), start=2):
        e.append(_entry("user", f"u{i}", "a1", _ts(minute), _say(f"Try {i}")))
        e.append(
            _entry("assistant", f"a{i}", f"u{i}", _ts(minute, 30), _say(f"Ans {i}"))
        )
    # A rewind inside branch u3: two prompts after a3.
    e.append(_entry("user", "n1", "a3", _ts(12), _say("Nested one")))
    e.append(_entry("assistant", "na1", "n1", _ts(13), _say("N1")))
    e.append(_entry("user", "n2", "a3", _ts(20), _say("Nested two")))
    e.append(_entry("assistant", "na2", "n2", _ts(21), _say("N2")))
    path = tmp_path / "forks" / "s1.jsonl"
    _write(path, e)
    return path.parent


class TestSyntheticForks:
    def test_many_branches_in_one_turn(self, tmp_path: Path) -> None:
        model, nodes = _model(_fork_tree(tmp_path))
        lanes = _branch_lanes(model)
        top = [lane for lane in lanes.values() if lane.parent_lane == MAIN_LANE]
        assert len(top) == 4  # five branches, the earliest continues main
        assert [lane.rank for lane in top] == [1, 2, 3, 4]
        assert len({lane.turn_index for lane in top}) == 1
        turn = nodes[top[0].turn_index or -1]
        assert turn.type == "user" and turn.lane_id == MAIN_LANE
        # Ranked by spawn time (shared fork point) then by first message.
        assert [lane.name for lane in top] == ["Try 3", "Try 4", "Try 5", "Try 6"]

    def test_fork_inside_a_fork(self, tmp_path: Path) -> None:
        model, nodes = _model(_fork_tree(tmp_path))
        lanes = _branch_lanes(model)
        outer = next(lane for lane in lanes.values() if lane.name == "Try 3")
        inner = [lane for lane in lanes.values() if lane.parent_lane == outer.lane_id]
        assert len(inner) == 1
        nested = inner[0]
        assert nested.name == "Nested two" and nested.depth == 2
        assert nodes[nested.spawn_index or -1].lane_id == outer.lane_id
        # Its turn is a turn of the outer branch, not of main.
        turn = nodes[nested.turn_index or -1]
        assert turn.lane_id == outer.lane_id and nested.rank == 1
        # "Nested one" (earliest) continues the outer fork lane.
        continued = {lane for lane, _fp in model.continuations.values()}
        assert continued == {MAIN_LANE, outer.lane_id}
        n1 = [
            n
            for n in nodes.values()
            if not n.is_session_header and "Nested one" in str(n.content)
        ]
        assert n1 and all(n.lane_id == outer.lane_id for n in n1)

    def test_fork_point_filtered_to_a_landmark(self, tmp_path: Path) -> None:
        """At a reduced detail level a fork point that would be filtered out
        stays as a fork-only box; the lanes still spawn from it."""
        tool_only = [{"type": "tool_use", "id": "toolu_g", "name": "Grep", "input": {}}]
        e = [
            _entry("user", "u0", None, _ts(0), _say("Go")),
            _entry("assistant", "a1", "u0", _ts(1), tool_only),
            _entry("user", "c1", "a1", _ts(3), _say("Branch one")),
            _entry("user", "c2", "a1", _ts(5), _say("Branch two")),
        ]
        _write(tmp_path / "fo" / "s1.jsonl", e)
        model, nodes = _model(tmp_path / "fo", depth=RenderingDepth.ASSISTANT)
        (lane,) = model.branches
        assert nodes[lane.spawn_index or -1].fork_only
        html = _minimal_html(tmp_path / "fo", depth=RenderingDepth.ASSISTANT)
        attrs = _card_attrs(html)
        box = attrs[f"d-{lane.spawn_index}"]
        assert box["data-spawns"] == lane.lane_id and box["data-lane"] == "main"
        assert f"<div class='fork-point' id='msg-d-{lane.spawn_index}'" in html


def _agent_session(tmp_path: Path) -> Path:
    """A visible three-deep chain (main → A → B → C), an agent spawned on a
    rewind branch, and an agent whose own transcript has a rewind."""
    sid = "a0000000-0000-4000-8000-000000000001"
    root = tmp_path / "agents"
    sdir = root / sid

    def ent(kind: str, uuid: str, parent: Optional[str], ts: str, content: Any, **kw):
        return _entry(kind, uuid, parent, ts, content, sid=sid, **kw)

    trunk = [
        ent("user", "u1", None, _ts(0), _say("Chain please")),
        ent("assistant", "a1", "u1", _ts(1), _spawn("t_A", "Agent A")),
        ent(
            "user",
            "r1",
            "a1",
            _ts(9),
            _result("t_A", "A reports", "agA"),
            toolUseResult={"agentId": "agA", "status": "completed"},
        ),
        ent("assistant", "a2", "r1", _ts(10), _say("Done; now rewindable")),
        # Rewind of a2: the earliest continues main, the later spawns agent F.
        ent("user", "u2", "a2", _ts(11), _say("Plain follow-up")),
        ent("assistant", "a3", "u2", _ts(12), _say("Fine")),
        ent("user", "u3", "a2", _ts(20), _say("Rewound: spawn F")),
        ent("assistant", "a4", "u3", _ts(21), _spawn("t_F", "Agent F")),
        ent(
            "user",
            "r4",
            "a4",
            _ts(25),
            _result("t_F", "F reports", "agF"),
            toolUseResult={"agentId": "agF", "status": "completed"},
        ),
        ent("assistant", "a5", "r4", _ts(26), _say("F answered")),
        # An agent whose transcript forks (a rewind inside the agent).
        ent("user", "u4", "a5", _ts(30), _say("Spawn R")),
        ent("assistant", "a6", "u4", _ts(31), _spawn("t_R", "Agent R")),
        ent(
            "user",
            "r6",
            "a6",
            _ts(39),
            _result("t_R", "R reports", "agR"),
            toolUseResult={"agentId": "agR", "status": "completed"},
        ),
    ]
    _write(root / f"{sid}.jsonl", trunk)

    def chain(agent: str, child: Optional[tuple[str, str, str]], minute: int):
        uid = agent.lower()
        rows = [
            ent("user", f"{uid}-u", None, _ts(minute), f"Do: {agent}", agent=agent),
            ent(
                "assistant",
                f"{uid}-t",
                f"{uid}-u",
                _ts(minute, 10),
                _say(f"{agent} is thinking it over"),
                agent=agent,
            ),
        ]
        last = f"{uid}-t"
        if child is not None:
            tool, desc, child_id = child
            rows += [
                ent(
                    "assistant",
                    f"{uid}-s",
                    last,
                    _ts(minute, 20),
                    _spawn(tool, desc),
                    agent=agent,
                ),
                ent(
                    "user",
                    f"{uid}-r",
                    f"{uid}-s",
                    _ts(minute + 1, 50),
                    _result(tool, f"{child_id} reports", child_id),
                    agent=agent,
                ),
            ]
            last = f"{uid}-r"
        rows.append(
            ent(
                "assistant",
                f"{uid}-f",
                last,
                _ts(minute + 2),
                _say(f"{agent} final words"),
                agent=agent,
            )
        )
        return rows

    _agent_file(
        sdir, "agA", "t_A", "Agent A", chain("agA", ("t_B", "Agent B", "agB"), 2)
    )
    _agent_file(
        sdir, "agB", "t_B", "Agent B", chain("agB", ("t_C", "Agent C", "agC"), 3)
    )
    _agent_file(sdir, "agC", "t_C", "Agent C", chain("agC", None, 4))
    _agent_file(sdir, "agF", "t_F", "Agent F", chain("agF", None, 22))
    rewind = [
        ent("user", "agr-u", None, _ts(32), "Do: Agent R", agent="agR"),
        ent("assistant", "agr-a", "agr-u", _ts(33), _say("R step one"), agent="agR"),
        ent("user", "agr-x", "agr-a", _ts(34), _say("first"), agent="agR"),
        ent("user", "agr-y", "agr-a", _ts(36), _say("second"), agent="agR"),
        ent("assistant", "agr-f", "agr-y", _ts(37), _say("R final"), agent="agR"),
    ]
    _agent_file(sdir, "agR", "t_R", "Agent R", rewind)
    return root


class TestSyntheticAgents:
    def test_visible_three_deep_chain(self, tmp_path: Path) -> None:
        model, nodes = _model(_agent_session(tmp_path))
        lanes = _branch_lanes(model)
        assert [
            lanes[k].parent_lane for k in ("agent-agA", "agent-agB", "agent-agC")
        ] == [
            MAIN_LANE,
            "agent-agA",
            "agent-agB",
        ]
        assert [lanes[k].depth for k in ("agent-agA", "agent-agB", "agent-agC")] == [
            1,
            2,
            3,
        ]
        c = lanes["agent-agC"]
        assert nodes[c.spawn_index or -1].lane_id == "agent-agB"
        assert nodes[c.merge_index or -1].lane_id == "agent-agB"
        # Same user turn for the whole chain, ranked by spawn time.
        chain = [lanes[k] for k in ("agent-agA", "agent-agB", "agent-agC")]
        assert len({lane.turn_index for lane in chain}) == 1
        assert [lane.rank for lane in chain] == [1, 2, 3]
        assert lanes["agent-agA"].stats == "3 steps · 48.4k tokens · 2m 13s"

    def test_agent_on_a_fork_branch(self, tmp_path: Path) -> None:
        model, nodes = _model(_agent_session(tmp_path))
        lanes = _branch_lanes(model)
        forks = [lane for lane in lanes.values() if lane.kind == "fork"]
        assert len(forks) == 1
        f_lane = lanes["agent-agF"]
        assert f_lane.parent_lane == forks[0].lane_id and f_lane.depth == 2
        # Its turn is the rewound prompt on the fork branch.
        turn = nodes[f_lane.turn_index or -1]
        assert turn.lane_id == forks[0].lane_id and "spawn F" in str(turn.content)

    def test_rewind_inside_an_agent_stays_in_the_agent(self, tmp_path: Path) -> None:
        """Agent lines never become branch pseudo-sessions (their render sid
        is the parent's), so an agent-internal rewind has no fork lane."""
        model, nodes = _model(_agent_session(tmp_path))
        assert not any("agR" in k for k in model.lanes if k.startswith("branch-"))
        members = [n for n in nodes.values() if n.lane_id == "agent-agR"]
        assert members and all(n.is_sidechain for n in members)


# ---------------------------------------------------------------- HTML


class TestHtmlAttributes:
    def test_classic_carries_no_lane_data(self) -> None:
        entries, tree = load_directory_transcripts(NESTED, silent=True)
        classic = generate_html(entries, "T", session_tree=tree)
        assert "data-lane" not in classic
        assert "data-spawns" not in classic and "data-merges" not in classic

    def test_every_card_has_a_lane(self) -> None:
        html = _minimal_html(NESTED)
        cards = re.findall(r"<div class='message [^>]*>", html)
        assert cards and all(' data-lane="' in card for card in cards)

    def test_head_attributes(self) -> None:
        attrs = _card_attrs(_minimal_html(ASYNC))
        heads = {k: v for k, v in attrs.items() if "data-lane-id" in v}
        assert list(heads.values()) == [
            {
                "data-lane": "main",
                "data-spawns": "agent-cccc333",
                "data-lane-id": "agent-cccc333",
                "data-lane-kind": "async-agent",
                "data-lane-name": "Coverage analysis",
                "data-lane-tag": "coverage",
                "data-lane-meta": "Explore · claude-opus-4-7 · async",
                "data-lane-parent": "main",
                "data-lane-depth": "1",
                "data-lane-from": "d-2",
                "data-lane-to": "d-9",
                "data-lane-turn": "d-1",
                "data-lane-rank": "1",
                "data-lane-stats": "1 step · 23.1k tokens · 15.5s",
                "data-lane-ts": "2026-04-19T10:04:00.000Z 2026-04-19T10:04:00.000Z",
            }
        ]
        assert attrs["d-9"] == {"data-lane": "main", "data-merges": "agent-cccc333"}
        assert attrs["d-5"] == {"data-lane": "agent-cccc333"}

    def test_fork_headers(self) -> None:
        attrs = _card_attrs(_minimal_html(TEST_DATA / "dag_within_fork.jsonl"))
        assert attrs["d-3"]["data-spawns"] == "branch-s1@d_prime"
        assert attrs["d-4"] == {
            "data-lane": "main",
            "data-lane-continues": "main",
            "data-lane-from": "d-3",
        }
        head = attrs["d-8"]
        assert head["data-lane"] == head["data-lane-id"] == "branch-s1@d_prime"
        assert head["data-lane-kind"] == "fork" and "data-lane-to" not in head
        assert head["data-lane-from"] == "d-3" and head["data-lane-turn"] == "d-3"
        assert {attrs[f"d-{i}"]["data-lane"] for i in (9, 10)} == {"branch-s1@d_prime"}

    def test_values_are_escaped(self) -> None:
        model = LaneModel()
        from claude_code_log.lanes import LaneInfo

        model.lanes["agent-x"] = LaneInfo(
            lane_id="agent-x",
            kind="agent",
            name="Say \"hi\" <now> & 'then'",
            spawn_index=1,
            head_index=1,
            cards=1,
            steps=1,
        )
        attrs = lane_attributes(model)
        from claude_code_log.html.minimal_theme import lane_attrs

        node = TemplateMessage.__new__(TemplateMessage)
        node.lane_id = "main"
        node.message_index = 1
        markup = str(lane_attrs(node, attrs))
        assert (
            'data-lane-name="Say &quot;hi&quot; &lt;now&gt; &amp; &#x27;then&#x27;"'
            in markup
        )

    def test_appended_notification_updates_the_head_card(self) -> None:
        """Live update patches changed cards in place: when an async agent's
        notification arrives, the spawn card's own attributes change (so its
        hash changes and it is swapped), and the merge card arrives marked."""
        trunk = ASYNC / "eb000000-0000-4000-8000-000000000001.jsonl"
        lines = trunk.read_text(encoding="utf-8").splitlines()
        cut = next(i for i, line in enumerate(lines) if "<task-notification>" in line)

        def render(rows: list[str], tmp: Path) -> dict[str, dict[str, str]]:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(ASYNC, tmp)
            (tmp / trunk.name).write_text("\n".join(rows) + "\n", encoding="utf-8")
            return _card_attrs(_minimal_html(tmp))

        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            before = render(lines[:cut], Path(scratch) / "a")
            after = render(lines, Path(scratch) / "b")
        assert "data-lane-to" not in before["d-2"]
        assert before["d-2"]["data-lane-kind"] == "async-agent"  # run_in_background
        assert after["d-2"]["data-lane-to"] == "d-9"
        assert after["d-9"]["data-merges"] == "agent-cccc333"


class TestTeammateLinks:
    """``teammate_links``: same-page anchors between a teammate exchange's
    two ends (P7, § 1.6.5), rendered by ``html/minimal_theme.cross_links``."""

    @staticmethod
    def _links(
        path: Path,
    ) -> tuple[dict[int, list[tuple[int, str]]], dict[int, TemplateMessage]]:
        entries, tree = load_directory_transcripts(path, silent=True)
        roots, _nav, _ctx = generate_template_messages(entries, session_tree=tree)
        nodes: dict[int, TemplateMessage] = {}
        stack = list(roots)
        while stack:
            node = stack.pop()
            if node.message_index is not None:
                nodes[node.message_index] = node
            stack.extend(node.children)
        return teammate_links(roots, annotate_lanes(roots)), nodes

    def test_both_directions(self, tmp_path: Path) -> None:
        links, nodes = self._links(write_team_demo(tmp_path / "team"))
        by_label: dict[str, tuple[int, int]] = {}
        for source, targets in links.items():
            for target, label in targets:
                by_label[label] = (source, target)
        assert set(by_label) == {
            "→ alice's thread",
            "→ received by team-lead",
            "← sent by alice",
            "→ received by alice",
            "← sent by team-lead",
        }
        # alice's SendMessage (her thread) ↔ the lead's <teammate-message>.
        sent, got = by_label["→ received by team-lead"]
        assert "#agent-" in (nodes[sent].meta.session_id or "")
        assert "#agent-" not in (nodes[got].meta.session_id or "")
        assert by_label["← sent by alice"] == (got, sent)
        # The lead's SendMessage ↔ the copy in alice's thread.
        sent, got = by_label["→ received by alice"]
        assert "#agent-" not in (nodes[sent].meta.session_id or "")
        assert "#agent-" in (nodes[got].meta.session_id or "")
        assert by_label["← sent by team-lead"] == (got, sent)

    def test_unmatched_messages_get_no_link(self) -> None:
        # The fixture's messages have no counterpart in the other thread;
        # only the spawn → thread links resolve.
        links, _nodes = self._links(TEAMMATES)
        labels = sorted(label for targets in links.values() for _t, label in targets)
        assert labels == ["→ alice's thread", "→ bob's thread"]

    def test_html(self, tmp_path: Path) -> None:
        html = _minimal_html(write_team_demo(tmp_path / "team"))
        assert html.count("class='mn-xlink'") == 5
        assert 'data-label="→ received by alice"' in html
        entries, tree = load_directory_transcripts(
            write_team_demo(tmp_path / "c"), silent=True
        )
        classic = generate_html(entries, session_tree=tree)
        assert "mn-xlink" not in classic


class TestAsyncResultAtMerge:
    """Minimal theme: an async agent's answer is shown on its
    ``<task-notification>`` (the merge row), not on the spawn (P7)."""

    def test_minimal_moves_the_answer(self) -> None:
        html = _minimal_html(ASYNC)
        assert '<div class="task-async-answer-label">' not in html
        assert "mn-async-jump" in html
        assert "task-notification-result" in html

    def test_classic_keeps_it_on_the_spawn(self) -> None:
        entries, tree = load_directory_transcripts(ASYNC, silent=True)
        html = generate_html(entries, session_tree=tree)
        assert '<div class="task-async-answer-label">' in html
        assert "mn-async-jump" not in html


# ---------------------------------------------------------------- paths


def _lane_project(tmp_path: Path) -> Path:
    """One project holding the async, nested and teammate fixtures plus a
    fork session — several sessions, so it paginates."""
    project = tmp_path / "projects" / "-tmp-lanes"
    project.mkdir(parents=True)
    for source in (ASYNC, NESTED, TEAMMATES):
        for item in source.iterdir():
            if item.suffix == ".md":
                continue
            dest = project / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy(item, dest)
    shutil.copy(TEST_DATA / "dag_compact_after_rewind.jsonl", project / "s1.jsonl")
    return project


def _lane_view(project: Path) -> dict[str, list[tuple[str, ...]]]:
    """Per output file: every lane-bearing card's attributes, id-free (pages
    and session files number their cards differently)."""
    view: dict[str, list[tuple[str, ...]]] = {}
    for page in sorted(project.glob("*.html")):
        attrs = _card_attrs(page.read_text(encoding="utf-8"))
        view[page.name] = sorted(
            tuple(
                f"{k}={v}"
                for k, v in sorted(a.items())
                if k not in {"data-lane-from", "data-lane-to", "data-lane-turn"}
                and not k.startswith("data-teammate-link")
            )
            for a in attrs.values()
            if set(a) - {"data-lane"} or a.get("data-lane") != "main"
        )
    return view


class TestRenderPaths:
    """The lane data is computed inside ``HtmlRenderer.generate``, which every
    path goes through; the same project must carry the same lanes whichever
    path wrote it."""

    def _convert(self, project: Path, **kwargs: Any) -> None:
        convert_jsonl_to(
            "html", project, silent=True, theme="minimal", page_size=8, **kwargs
        )

    def test_paths_agree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from claude_code_log import render_dispatch, render_pool

        reference = _lane_project(tmp_path / "ref")
        self._convert(reference, render_jobs=1)
        expected = _lane_view(reference)
        assert "combined_transcripts_2.html" in expected, "fixture should paginate"
        session_views = {k: v for k, v in expected.items() if k.startswith("session-")}
        lanes_seen = {
            item for rows in session_views.values() for row in rows for item in row
        }
        for lane in (
            "data-lane-id=agent-cccc333",
            "data-lane-id=agent-nsleaf22",
            "data-lane-id=branch-s1@b2u",
        ):
            assert lane in lanes_seen

        # Streaming (forced), page by page.
        streamed = _lane_project(tmp_path / "stream")
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        self._convert(streamed, render_jobs=1)  # warms the cache
        for page in streamed.glob("*.html"):
            page.unlink()
        self._convert(streamed, render_jobs=1)
        monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING")
        assert _lane_view(streamed) == expected

        # Parallel workers.
        monkeypatch.setattr(render_dispatch, "_MIN_MESSAGES_FOR_RENDER_POOL", 0)
        monkeypatch.setattr(render_dispatch, "_MIN_ENTRIES_FOR_RENDER_POOL", 0)
        monkeypatch.setattr(render_pool, "available_memory_bytes", lambda: 64 * 1024**3)
        dispatched: list[str] = []
        submit = render_pool.RenderPool.submit

        def counting_submit(pool: Any, unit: Any) -> Any:
            future = submit(pool, unit)
            if future is not None:
                dispatched.append(unit.kind)
            return future

        monkeypatch.setattr(render_pool.RenderPool, "submit", counting_submit)
        pooled = _lane_project(tmp_path / "pool")
        self._convert(pooled, render_jobs=2)
        assert set(dispatched) == {"page", "session"}, "the pool never rendered"
        assert _lane_view(pooled) == expected

        # Session-scoped incremental: drop the session files, keep the
        # (current) combined output, regenerate with --combined no.
        scoped = _lane_project(tmp_path / "scoped")
        self._convert(scoped, render_jobs=1)
        for page in scoped.glob("session-*.html"):
            page.unlink()
        self._convert(scoped, render_jobs=1, write_combined=False)
        assert _lane_view(scoped) == expected


# ------------------------------------------------- live sessions (P7b)

from test.dag_live_fixture import (  # noqa: E402
    AGENT_A,
    AGENT_C,
    LANE_A,
    LANE_C,
    LIVE_SESSION,
    STAGES,
    LiveDagScript,
)

LIVE_PAGES = (f"session-{LIVE_SESSION}.html", "combined_transcripts.html")


def _heads(html: str) -> dict[str, dict[str, str]]:
    """Lane id → its head card's attributes."""
    return {
        a["data-lane-id"]: a for a in _card_attrs(html).values() if "data-lane-id" in a
    }


class TestLaneState:
    """An agent lane without a merge row is ``open`` (may still be running)
    unless the page proves it is over (``ended``); the client decides
    whether an open lane reads as running (dev-docs/minimal-theme.md § 8)."""

    def _render(self, script: LiveDagScript) -> dict[str, dict[str, str]]:
        convert_jsonl_to("html", script.project, silent=True, theme="minimal")
        return _heads((script.project / LIVE_PAGES[0]).read_text(encoding="utf-8"))

    def test_running_agents_are_open_until_they_merge(self, tmp_path: Path) -> None:
        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        heads = self._render(script)
        assert heads[LANE_A]["data-lane-state"] == "open"
        assert heads[LANE_C]["data-lane-state"] == "open"
        assert "data-lane-to" not in heads[LANE_C]
        script.run_to("c_merges")
        heads = self._render(script)
        assert "data-lane-state" not in heads[LANE_C]  # merged: no state
        assert heads[LANE_A]["data-lane-state"] == "open"
        script.run_to("a_merges")
        heads = self._render(script)
        assert not any("data-lane-state" in head for head in heads.values())

    def test_a_sync_agent_whose_parent_moved_on_has_ended(self, tmp_path: Path) -> None:
        """The parent blocks on a synchronous call: a later model step there
        means the call is over, result or not (a crash, a lost result)."""
        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        script._append(
            script.trunk,
            [
                script._entry(
                    "assistant",
                    script.last["main"],
                    30,
                    _text_block("The check never came back; moving on."),
                )
            ],
        )
        heads = self._render(script)
        assert heads[LANE_C]["data-lane-state"] == "ended"
        # The async agent is not waited on: still open.
        assert heads[LANE_A]["data-lane-state"] == "open"

    def test_another_sessions_activity_proves_nothing(self, tmp_path: Path) -> None:
        """A combined page holds every session in one main lane: a second
        session moving on later must not end the first one's running call."""
        from datetime import timedelta

        from test.dag_demo_fixture import BASE

        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        other = LiveDagScript(
            script.project,
            base=BASE + timedelta(hours=1),
            session="dade0000-0000-4000-8000-0000000000c2",
            suffix="two",
            uid_space=5,
        )
        other.run_to("c_merges")
        convert_jsonl_to("html", script.project, silent=True, theme="minimal")
        heads = _heads(
            (script.project / "combined_transcripts.html").read_text(encoding="utf-8")
        )
        assert heads[LANE_C]["data-lane-state"] == "open"
        assert "data-lane-state" not in heads[LANE_C + "two"]

    def test_steering_does_not_end_a_running_agent(self, tmp_path: Path) -> None:
        """A message the user types while tools run is steering, not a new
        turn: the agent is still running."""
        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        steer = {
            "type": "attachment",
            "uuid": "steer-0001",
            "parentUuid": script.last["main"],
            "isSidechain": False,
            "userType": "external",
            "cwd": "/home/synthetic/theme",
            "sessionId": LIVE_SESSION,
            "version": "2.1.180",
            "timestamp": script.time(8),
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
                "timestamp": script.time(8),
                "prompt": "Also look at the legend colours.",
            },
        }
        script._append(script.trunk, [steer])
        convert_jsonl_to("html", script.project, silent=True, theme="minimal")
        html = (script.project / LIVE_PAGES[0]).read_text(encoding="utf-8")
        assert "Also look at the legend colours." in html
        assert _heads(html)[LANE_C]["data-lane-state"] == "open"

    def test_a_lane_nested_in_a_finished_one_has_ended(self, tmp_path: Path) -> None:
        """C spawns D; D never returns, then C answers and merges: D can't
        still be running inside a finished agent."""
        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        nested, tool = "d0live0nest", "toolu_live_nestD"
        spawn = script._entry(
            "assistant",
            script.last[AGENT_C],
            8,
            [
                {
                    "type": "tool_use",
                    "id": tool,
                    "name": "Task",
                    "input": {
                        "description": "Read the legend",
                        "prompt": "Read the legend.",
                        "subagent_type": "Explore",
                    },
                }
            ],
            agent=AGENT_C,
        )
        script._agent(AGENT_C, [spawn])
        script._meta(nested, tool, "Read the legend")
        script._agent(
            nested, [script._entry("user", None, 8.5, "Read the legend.", agent=nested)]
        )
        script._agent(
            nested,
            script._calls(nested, [(9, "Read", {"file_path": "legend.css"}, "ok")]),
        )
        heads = self._render(script)
        assert heads[f"agent-{nested}"]["data-lane-parent"] == LANE_C
        assert heads[f"agent-{nested}"]["data-lane-state"] == "open"
        script.run_to("c_merges")
        heads = self._render(script)
        assert "data-lane-state" not in heads[LANE_C]
        assert heads[f"agent-{nested}"]["data-lane-state"] == "ended"

    def test_a_stopped_background_agent_has_ended(self, tmp_path: Path) -> None:
        """A ``TaskStop`` that reports the agent stopped ends its lane (P7c:
        the client no longer guesses from elapsed time, so the page has to
        say it). A stop that found nothing proves nothing."""
        from test.dag_demo_fixture import _result, _tool

        def stop(script: LiveDagScript, tool: str, at: float, ok: bool) -> None:
            use = script._entry(
                "assistant",
                script.last["main"],
                at,
                _tool(tool, "TaskStop", {"task_id": AGENT_A}),
            )
            text = (
                f"Successfully stopped task: {AGENT_A} (audit)"
                if ok
                else f"Error: No task found with ID: {AGENT_A}"
            )
            res = script._entry("user", use["uuid"], at + 1, _result(tool, text))
            res["toolUseResult"] = {"message": text} if ok else text
            script._append(script.trunk, [use, res])
            script.last["main"] = res["uuid"]

        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        stop(script, "toolu_live_stop_miss", 20, ok=False)
        assert self._render(script)[LANE_A]["data-lane-state"] == "open"
        stop(script, "toolu_live_stop_hit", 25, ok=True)
        assert self._render(script)[LANE_A]["data-lane-state"] == "ended"

    def test_classic_carries_no_state(self, tmp_path: Path) -> None:
        script = LiveDagScript(tmp_path / "p")
        script.run_to("start")
        convert_jsonl_to("html", script.project, silent=True, theme="classic")
        html = (script.project / LIVE_PAGES[0]).read_text(encoding="utf-8")
        assert "data-lane" not in html


def _text_block(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


class TestLiveGrowth:
    """A session growing on disk the way a live one does — a running agent's
    transcript growing while the trunk sits still, results arriving, a
    rewind — re-converted incrementally after every stage, the way
    ``serve --watch`` (session files: session-scoped regeneration, an entry
    store held across ticks) and ``watch --combined yes`` (both pages, a
    store) do, must carry exactly the lane data a cold conversion of the
    same files produces. Before P7b neither path saw a running agent grow:
    the trunk's cache row and held entries were pinned to the trunk file
    alone, so the agent's block stayed one tick behind until the trunk
    changed."""

    @staticmethod
    def _fresh(tmp_path: Path, stage: str) -> dict[str, dict[str, dict[str, str]]]:
        script = LiveDagScript(tmp_path / f"fresh-{stage}" / "projects" / "-tmp-live")
        script.run_to(stage)
        convert_jsonl_to("html", script.project, silent=True, theme="minimal")
        return {
            name: _card_attrs((script.project / name).read_text(encoding="utf-8"))
            for name in LIVE_PAGES
        }

    @pytest.mark.parametrize("store", [True, False], ids=["store", "no-store"])
    def test_serve_watch_session_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: bool
    ) -> None:
        from claude_code_log import converter
        from claude_code_log.converter import process_projects_hierarchy
        from claude_code_log.entry_store import ParsedEntryStore

        used: dict[str, int] = {"scoped": 0, "refresh": 0}
        scoped = converter._load_stale_session_transcripts
        refresh = converter._incremental_cache_refresh

        def spy_scoped(*args: Any, **kwargs: Any) -> Any:
            result = scoped(*args, **kwargs)
            used["scoped"] += result is not None
            return result

        def spy_refresh(*args: Any, **kwargs: Any) -> Any:
            result = refresh(*args, **kwargs)
            used["refresh"] += bool(result)
            return result

        monkeypatch.setattr(converter, "_load_stale_session_transcripts", spy_scoped)
        monkeypatch.setattr(converter, "_incremental_cache_refresh", spy_refresh)

        projects = tmp_path / "live" / "projects"
        script = LiveDagScript(projects / "-tmp-live")
        script.run_to("start")
        process_projects_hierarchy(projects, silent=True, theme="minimal")
        held = ParsedEntryStore() if store else None
        for stage in STAGES[1:]:
            script.advance(stage)
            # cli.serve's `reconvert`, verbatim.
            process_projects_hierarchy(
                projects,
                silent=True,
                write_combined=False,
                entry_store=held,
                theme="minimal",
            )
            page = LIVE_PAGES[0]
            got = _card_attrs((script.project / page).read_text(encoding="utf-8"))
            assert got == self._fresh(tmp_path, stage)[page], stage
        assert used["scoped"] and used["refresh"], used
        if held is not None:
            assert held.prefix_hits, "the trunk's parse never resumed"

    def test_watch_combined_pages(self, tmp_path: Path) -> None:
        from claude_code_log.entry_store import ParsedEntryStore

        script = LiveDagScript(tmp_path / "live" / "projects" / "-tmp-live")
        script.run_to("start")
        held = ParsedEntryStore()
        convert_jsonl_to(
            "html", script.project, silent=True, entry_store=held, theme="minimal"
        )
        for stage in STAGES[1:]:
            script.advance(stage)
            # cli.watch's `convert` for `--combined yes`.
            convert_jsonl_to(
                "html",
                script.project,
                silent=True,
                write_combined=True,
                generate_individual_sessions=True,
                entry_store=held,
                theme="minimal",
            )
            fresh = self._fresh(tmp_path, stage)
            for page in LIVE_PAGES:
                got = _card_attrs((script.project / page).read_text(encoding="utf-8"))
                assert got == fresh[page], (stage, page)

    def test_the_running_agent_grows_on_the_page(self, tmp_path: Path) -> None:
        """The case the equality above exists for, spelled out."""
        from claude_code_log.converter import process_projects_hierarchy
        from claude_code_log.entry_store import ParsedEntryStore

        projects = tmp_path / "projects"
        script = LiveDagScript(projects / "-tmp-live")
        script.run_to("start")
        process_projects_hierarchy(projects, silent=True, theme="minimal")
        held = ParsedEntryStore()
        steps = []
        for stage in ("c_grows", "c_grows_more", "c_merges"):
            script.advance(stage)
            process_projects_hierarchy(
                projects,
                silent=True,
                write_combined=False,
                entry_store=held,
                theme="minimal",
            )
            html = (script.project / LIVE_PAGES[0]).read_text(encoding="utf-8")
            steps.append(_heads(html)[LANE_C]["data-lane-stats"].split(" · ")[0])
        assert steps == ["2 steps", "3 steps", "3 steps"]

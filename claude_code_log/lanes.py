"""Branch lanes: sub-agents and rewind forks as one format-neutral model.

The minimal HTML theme draws a transcript as a DAG: the main conversation
on one rail lane, every sub-agent transcript and every rewind fork on a lane
of its own that leaves the main line at a *spawn* row and (for agents)
rejoins it at a *merge* row (work/minimal-theme-dag.md § 1.6). The DOM keeps
the nested render tree untouched; this module only works out, server side,
which lane each node belongs to and what every lane looks like, so the
client never has to re-derive structure from card classes.

``annotate_lanes(roots)`` walks a rendered tree (after the HTML renderer's
formatting pass, so ``should_render`` is known), sets
``TemplateMessage.lane_id`` on every node and returns a ``LaneModel``. It
reads only render-tree fields, so a Markdown or JSON renderer could use it
too; turning the model into ``data-*`` attributes is the HTML theme's job
(``html/minimal_theme.lane_attributes``).

Lane rules (§ 3.3 of the plan, as built):

- **Agents** — a card with ``meta.is_sidechain`` and a ``{trunk}#agent-<id>``
  session id is in lane ``agent-<id>``. Its spawn row is the ``Task`` /
  ``Agent`` tool_use whose result carries the agent id
  (``renderer.spawned_agent_id_of``, shared with the block relocation);
  its merge row is that tool_result for a sync agent and the
  ``<task-notification>`` card for an async one. Nesting (#213) falls out:
  a nested spawn card sits in its parent agent's lane, so the child lane's
  ``parent_lane`` is that agent.
- **Teammates** are *not* branches (§ 7 decision 4): a teammate thread stays
  in its spawner's lane. The model still records it (``kind="teammate"``)
  so the spawn card can link to the thread's first card.
- **Workflow agents** (#174) — the phase and agent cards spliced under a
  Workflow call stay in the call's lane, as rows. Each agent card whose
  side-channel transcript rendered owns lane ``wfagent-<agentId>``
  (``kind="workflow-agent"``): its transcript's cards. The lane is spawned
  at the agent card's parent — its phase card, or the Workflow call's
  result when the run has no phase grouping — and merges at the agent card
  itself, which reports the agent's result. An agent without a transcript
  stays a plain row. Everything inside a workflow agent's transcript stays
  in its lane (no nested lanes there).
- **Forks** — at a rewind every child becomes a branch pseudo-session
  (dev-docs/dag.md § 7). The **earliest** branch continues its fork point's
  lane (§ 7 decision 3; its header is recorded in ``continuations``); every
  later one is lane ``branch-<branch sid>``, spawned at the fork-point card
  and never merged. A fork inside a fork lane nests the same way.
- Everything else is ``main``.

A lane exists only once it has a rendered card: a sub-agent whose
transcript deduplicated into its spawn pair (#213), or was stripped at a
reduced detail level, renders as an ordinary tool call.

An agent lane **without a merge row** is either still running (a live
session, P7b) or ended without one (a crash, a killed background agent).
Only what is on the page decides, so the render stays a pure function of the
transcript: ``state`` is ``"ended"`` when the page proves the agent is over —
a *synchronous* agent's parent line moved on past its spawn (the parent
blocks on a synchronous call, so a later model step or prompt there means the
call is over), a ``TaskStop`` of the agent's id reported it stopped (P7c), or
the lane is nested in a lane that merged or ended — and ``"open"``
otherwise. (A killed or failed background agent still gets its
``<task-notification>``, which is its merge row.) Merged lanes and forks
have no state. Whether an open lane is shown as *running* is the client's
call (served live; dev-docs/minimal-theme.md § 8), so a static page of a
finished session never shows one running.

Each branch lane also carries the **user turn** it belongs to — the
top-level card of the conversation (a direct child of a session or branch
header) that contains its spawn row — and its 1-based **rank** in that
turn by spawn time. The client caps interleaving at three per turn and
puts the rest behind "+N more branches" (§ 1.6.3, § 7 decision 5).
Workflow agent lanes are ranked in their **group** instead — the phase
(or the phase-less run) that spawns them, ``<runId>/<phase ordinal>`` — so
a workflow fanning out to many agents gets its own cap and "+N more
agents", and never pushes the turn's other branches out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .models import (
    AssistantTextMessage,
    CompactedSummaryMessage,
    TaskInput,
    TaskNotificationMessage,
    TaskOutput,
    TaskStopInput,
    TaskStopOutput,
    ThinkingMessage,
    ToolResultMessage,
    ToolUseMessage,
    UserSteeringMessage,
    UserTextMessage,
    WorkflowAgentMessage,
)
from .utils import compact_count, format_duration, parse_timestamp

if TYPE_CHECKING:
    from .renderer import TemplateMessage

MAIN_LANE = "main"

# Node types grafted under a Workflow tool_use (#174): rows of their
# parent's lane (an agent card owns its transcript's lane).
_WORKFLOW_TYPES = frozenset({"workflow_phase", "workflow_agent"})
WORKFLOW_AGENT_KIND = "workflow-agent"
WORKFLOW_LANE_PREFIX = "wfagent-"
# Workflow agent states that say it is no longer running.
_WORKFLOW_RUNNING_STATES = frozenset(
    {"", "running", "pending", "started", "queued", "in_progress", "launched"}
)


@dataclass
class LaneInfo:
    """One branch lane (or, for ``kind="teammate"``, one teammate thread)."""

    lane_id: str  # "agent-<agentId>" | "wfagent-<agentId>" | "branch-<branch sid>"
    kind: str  # "agent" | "async-agent" | "workflow-agent" | "fork" | "teammate"
    name: (
        str  # Task description / workflow agent label / teammate name / branch preview
    )
    parent_lane: str = MAIN_LANE  # enclosing lane ("main" or a lane id)
    depth: int = 1  # 1 = opened from main; +1 per enclosing lane
    tag: str = ""  # one short word for the gutter of interleaved rows
    meta: str = ""  # "Explore · claude-sonnet-4 · async" / "rewind"
    spawn_index: Optional[int] = None  # card that opens the lane
    merge_index: Optional[int] = None  # card that closes it (None: forks)
    head_index: Optional[int] = None  # card carrying the lane's description
    first_index: Optional[int] = None  # the lane's first rendered card
    turn_index: Optional[int] = None  # user turn the spawn row belongs to
    # Workflow agents: "<runId>/<phase ordinal>" (or "<runId>"), the group
    # they are capped and ranked in instead of the turn; "" otherwise.
    group: str = ""
    rank: int = 0  # 1-based order within the turn / group (0: teammates)
    cards: int = 0  # rendered cards in the lane
    steps: int = 0  # rendered cards, the second half of each pair excluded
    total_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    agent_id: Optional[str] = None  # agents and teammates
    branch_session_id: Optional[str] = None  # forks
    # Agents without a merge row: "open" (may still be running) or "ended"
    # (the page proves it is over); "" for merged lanes, forks, teammates.
    state: str = ""

    @property
    def is_branch(self) -> bool:
        """True for a DAG branch; teammate threads are not branches."""
        return self.kind != "teammate"

    @property
    def stats(self) -> str:
        """``"6 steps · 48.4k tokens · 2m 13s"`` (absent parts dropped)."""
        parts = [f"{self.steps} step{'' if self.steps == 1 else 's'}"]
        if self.total_tokens:
            parts.append(f"{compact_count(self.total_tokens)} tokens")
        if self.duration_ms:
            parts.append(format_duration(self.duration_ms / 1000.0))
        return " · ".join(parts)


@dataclass
class LaneModel:
    """What ``annotate_lanes`` found in one rendered tree."""

    # Branch lanes and teammate threads, in spawn (DOM) order.
    lanes: dict[str, LaneInfo] = field(default_factory=lambda: {})
    # Branch headers that continue their fork point's lane (§ 7 decision 3):
    # header message_index → (lane continued, fork-point message_index).
    continuations: dict[int, tuple[str, Optional[int]]] = field(
        default_factory=lambda: {}
    )

    @property
    def branches(self) -> list[LaneInfo]:
        return [lane for lane in self.lanes.values() if lane.is_branch]

    @property
    def teammates(self) -> list[LaneInfo]:
        return [lane for lane in self.lanes.values() if not lane.is_branch]


@dataclass
class _Spawn:
    """The cards an agent id was spawned and returned at."""

    spawn: Optional["TemplateMessage"] = None
    result: Optional["TemplateMessage"] = None


def _task_input(node: Optional["TemplateMessage"]) -> Optional[TaskInput]:
    if node is not None and isinstance(node.content, ToolUseMessage):
        tool_input = node.content.input
        if isinstance(tool_input, TaskInput):
            return tool_input
    return None


def _is_card(node: "TemplateMessage") -> bool:
    """A rendered ``.message`` card that counts as a step of its lane."""
    return node.should_render and not node.fork_only and not node.is_session_header


def _short_tag(name: str, fallback: str) -> str:
    """The first word of a lane name, lower-cased, for the gutter tag."""
    for word in name.split():
        word = "".join(ch for ch in word if ch.isalnum() or ch in "-_")
        if word:
            return word.lower()[:12]
    return fallback


def _ts_sort_key(ts: Optional[str]) -> float:
    parsed = parse_timestamp(ts)
    return parsed.timestamp() if parsed is not None else float("inf")


def annotate_lanes(roots: list["TemplateMessage"]) -> LaneModel:
    """Set ``TemplateMessage.lane_id`` on every node; return the lane model.

    ``roots`` is the render tree (session headers with ``children``) as the
    HTML renderer formats it. The walk is iterative and linear in the
    number of nodes.
    """
    from .renderer import spawned_agent_id_of

    # -- 1. Flatten in DOM (pre-order) order, with parent, turn and header.
    order: list[tuple["TemplateMessage", Optional["TemplateMessage"]]] = []
    stack: list[tuple["TemplateMessage", Optional["TemplateMessage"]]] = [
        (root, None) for root in reversed(roots)
    ]
    while stack:
        node, parent = stack.pop()
        order.append((node, parent))
        for child in reversed(node.children):
            stack.append((child, node))

    position: dict[int, int] = {}
    by_index: dict[int, "TemplateMessage"] = {}
    turn_of: dict[int, Optional["TemplateMessage"]] = {}
    header_of: dict[int, Optional["TemplateMessage"]] = {}
    in_workflow: dict[int, bool] = {}
    parent_of: dict[int, Optional["TemplateMessage"]] = {}
    # The workflow agent card whose transcript holds a node (None outside
    # one; an agent card itself is a row of its parent's lane).
    wf_owner: dict[int, Optional["TemplateMessage"]] = {}
    for pos, (node, parent) in enumerate(order):
        key = id(node)
        position[key] = pos
        parent_of[key] = parent
        if parent is None:
            wf_owner[key] = None
        elif isinstance(parent.content, WorkflowAgentMessage):
            wf_owner[key] = parent
        else:
            wf_owner[key] = wf_owner[id(parent)]
        if node.message_index is not None:
            by_index[node.message_index] = node
        if node.is_session_header:
            turn_of[key] = None
            header_of[key] = node
        else:
            turn_of[key] = (
                node
                if parent is None or parent.is_session_header
                else turn_of[id(parent)]
            )
            header_of[key] = header_of[id(parent)] if parent is not None else None
        in_workflow[key] = (
            node.in_workflow_sidechannel
            or node.type in _WORKFLOW_TYPES
            or (parent is not None and in_workflow[id(parent)])
        )

    # -- 2. Spawn anchors, async notifications, branch groups.
    spawns: dict[str, _Spawn] = {}
    notif_by_task: dict[str, "TemplateMessage"] = {}
    notif_by_spawn: dict[int, "TemplateMessage"] = {}
    first_ts_of_header: dict[int, datetime] = {}
    for node, _parent in order:
        content = node.content
        if isinstance(content, TaskNotificationMessage):
            if content.task_id:
                notif_by_task.setdefault(content.task_id, node)
            if content.spawning_task_message_index is not None:
                notif_by_spawn.setdefault(content.spawning_task_message_index, node)
        header = header_of[id(node)]
        if header is not None and not node.is_session_header:
            parsed = parse_timestamp(node.meta.timestamp if node.meta else None)
            if parsed is not None:
                seen = first_ts_of_header.get(id(header))
                if seen is None or parsed < seen:
                    first_ts_of_header[id(header)] = parsed
        if in_workflow[id(node)]:
            continue
        agent_id = spawned_agent_id_of(node)
        if not agent_id:
            continue
        rec = spawns.setdefault(agent_id, _Spawn())
        if isinstance(content, ToolResultMessage):
            rec.result = node
            partner = (
                by_index.get(node.pair_first) if node.pair_first is not None else None
            )
            if partner is not None and (
                rec.spawn is None or _task_input(rec.spawn) is None
            ):
                rec.spawn = partner
            elif rec.spawn is None:
                rec.spawn = node
        elif _task_input(node) is not None or rec.spawn is None:
            # No tool_result (interrupted spawn): the loader stamps the id on
            # the tool_use's entry, whose text and tool_use cards share it.
            rec.spawn = node

    # The earliest branch at each rewind continues its fork point's lane.
    groups: dict[str, list["TemplateMessage"]] = {}
    for node, _parent in order:
        if not node.is_branch_header:
            continue
        hc = node.content
        key = (
            f"uuid:{getattr(hc, 'attachment_uuid', None)}"
            if getattr(hc, "attachment_uuid", None)
            else f"idx:{getattr(hc, 'parent_message_index', None)}"
        )
        groups.setdefault(key, []).append(node)
    continuing: set[int] = set()
    for members in groups.values():
        earliest = min(
            members,
            key=lambda h: (
                first_ts_of_header[id(h)].timestamp()
                if id(h) in first_ts_of_header
                else float("inf"),
                position[id(h)],
            ),
        )
        continuing.add(id(earliest))

    # -- 3. Assign lanes in DOM order.
    model = LaneModel()
    lanes = model.lanes
    lane_of: dict[int, str] = {}
    branch_lane_of_sid: dict[str, str] = {}
    wf_lane_of: dict[int, str] = {}  # id(agent card) -> its lane
    teammate_ids: set[str] = set()
    for agent_id, rec in spawns.items():
        task = _task_input(rec.spawn)
        if task is not None and task.team_name and task.name:
            teammate_ids.add(agent_id)

    def parent_lane_of(node: Optional["TemplateMessage"], default: str) -> str:
        return lane_of.get(id(node), default) if node is not None else default

    for node, parent in order:
        inherited = lane_of[id(parent)] if parent is not None else MAIN_LANE
        content = node.content
        if node.is_branch_header:
            fp_index = getattr(content, "parent_message_index", None)
            fork_point = by_index.get(fp_index) if fp_index is not None else None
            branch_sid = node.render_session_id
            parent_lane = parent_lane_of(
                fork_point,
                branch_lane_of_sid.get(
                    getattr(content, "parent_session_id", None) or "", MAIN_LANE
                ),
            )
            if id(node) in continuing:
                lane = parent_lane
                if node.message_index is not None:
                    model.continuations[node.message_index] = (
                        parent_lane,
                        fork_point.message_index if fork_point is not None else None,
                    )
            else:
                lane = f"branch-{branch_sid}"
                preview = getattr(content, "preview", None) or ""
                lanes[lane] = LaneInfo(
                    lane_id=lane,
                    kind="fork",
                    name=preview or f"Branch {branch_sid.rsplit('@', 1)[-1][:8]}",
                    parent_lane=parent_lane,
                    tag="fork",
                    meta="rewind",
                    spawn_index=(
                        fork_point.message_index if fork_point is not None else None
                    ),
                    head_index=node.message_index,
                    branch_session_id=branch_sid,
                )
            branch_lane_of_sid[branch_sid] = lane
        elif node.is_session_header:
            lane = MAIN_LANE
        elif in_workflow[id(node)]:
            owner = wf_owner[id(node)]
            if owner is None:
                lane = inherited
            elif id(owner) in wf_lane_of:
                lane = wf_lane_of[id(owner)]
            else:
                lane = _new_workflow_lane(
                    owner, parent_of.get(id(owner)), lane_of, lanes
                )
                wf_lane_of[id(owner)] = lane
        elif node.is_sidechain and "#agent-" in (node.meta.session_id or ""):
            agent_id = node.meta.session_id.rsplit("#agent-", 1)[-1]
            rec = spawns.get(agent_id)
            spawn = rec.spawn if rec is not None else None
            if agent_id in teammate_ids:
                lane = parent_lane_of(spawn, inherited)
                lane_key = f"agent-{agent_id}"
                if lane_key not in lanes:
                    task = _task_input(spawn)
                    lanes[lane_key] = LaneInfo(
                        lane_id=lane_key,
                        kind="teammate",
                        name=(task.name if task is not None else None) or agent_id,
                        parent_lane=lane,
                        spawn_index=spawn.message_index if spawn else None,
                        agent_id=agent_id,
                    )
            else:
                lane = f"agent-{agent_id}"
                if lane not in lanes:
                    lanes[lane] = LaneInfo(
                        lane_id=lane,
                        kind="agent",
                        name=agent_id,
                        parent_lane=parent_lane_of(spawn, inherited),
                        spawn_index=spawn.message_index if spawn else None,
                        agent_id=agent_id,
                    )
        else:
            lane = branch_lane_of_sid.get(node.render_session_id, MAIN_LANE)
        lane_of[id(node)] = lane
        node.lane_id = lane

    # -- 4. Per-lane cards, steps and time span.
    first_dt: dict[str, datetime] = {}
    last_dt: dict[str, datetime] = {}
    for node, _parent in order:
        if not _is_card(node):
            continue
        lane_key = node.lane_id
        sid = node.meta.session_id or ""
        if (
            node.is_sidechain
            and "#agent-" in sid
            and not in_workflow[id(node)]
            and sid.rsplit("#agent-", 1)[-1] in teammate_ids
        ):
            lane_key = f"agent-{sid.rsplit('#agent-', 1)[-1]}"
        info = lanes.get(lane_key)
        if info is None:
            continue
        info.cards += 1
        if not node.is_last_in_pair:
            info.steps += 1
        if info.first_index is None:
            info.first_index = node.message_index
        ts = node.meta.timestamp if node.meta else None
        parsed = parse_timestamp(ts)
        if parsed is not None and ts:
            if lane_key not in first_dt or parsed < first_dt[lane_key]:
                first_dt[lane_key] = parsed
                info.first_ts = ts
            if lane_key not in last_dt or parsed >= last_dt[lane_key]:
                last_dt[lane_key] = parsed
                info.last_ts = ts

    # -- 5. Drop lanes with nothing rendered; re-home their nodes.
    empty = {key for key, info in lanes.items() if info.cards == 0}

    def resolve(lane: str) -> str:
        seen: set[str] = set()
        while lane in empty and lane not in seen:
            seen.add(lane)
            lane = lanes[lane].parent_lane
        return lane

    if empty:
        for node, _parent in order:
            if node.lane_id in empty:
                node.lane_id = resolve(node.lane_id)
                lane_of[id(node)] = node.lane_id
        for header_index, (lane, fp) in list(model.continuations.items()):
            model.continuations[header_index] = (resolve(lane), fp)
        for key in empty:
            del lanes[key]
        for info in lanes.values():
            info.parent_lane = resolve(info.parent_lane)

    # -- 6. Merge rows, kinds, names and accounting.
    for info in lanes.values():
        if info.kind == WORKFLOW_AGENT_KIND:
            _describe_workflow_lane(info, by_index)
        elif info.kind != "fork":
            rec = spawns.get(info.agent_id or "")
            spawn = rec.spawn if rec is not None else None
            result = rec.result if rec is not None else None
            task = _task_input(spawn)
            notif = notif_by_task.get(info.agent_id or "")
            if notif is None and spawn is not None and spawn.message_index is not None:
                notif = notif_by_spawn.get(spawn.message_index)
            if notif is not None and not notif.should_render:
                notif = None
            is_async = bool(task is not None and task.run_in_background) or (
                notif is not None
            )
            if info.kind != "teammate":
                info.kind = "async-agent" if is_async else "agent"
                merge = notif if is_async else result
                info.merge_index = (
                    merge.message_index
                    if merge is not None and merge.should_render
                    else None
                )
                if task is not None:
                    info.name = task.description or task.subagent_type or info.name
            info.head_index = (
                spawn.message_index if spawn is not None else info.first_index
            )
            meta_parts: list[str] = []
            if task is not None and task.subagent_type:
                meta_parts.append(task.subagent_type)
            model_name = (spawn.display_model if spawn is not None else None) or (
                task.model if task is not None else None
            )
            if model_name:
                meta_parts.append(model_name)
            if is_async:
                meta_parts.append("async")
            info.meta = " · ".join(meta_parts)
            # Accounting: the notification's usage for async agents, the
            # result tail's metadata otherwise (either may be partial).
            accounts: list[tuple[Optional[int], Optional[int]]] = []
            if notif is not None and isinstance(notif.content, TaskNotificationMessage):
                usage = notif.content.usage
                if usage is not None:
                    accounts.append((usage.total_tokens, usage.duration_ms))
            if result is not None and isinstance(result.content, ToolResultMessage):
                output = result.content.output
                if isinstance(output, TaskOutput) and output.metadata is not None:
                    accounts.append(
                        (output.metadata.total_tokens, output.metadata.duration_ms)
                    )
            for tokens, duration in accounts:
                if info.total_tokens is None and tokens:
                    info.total_tokens = tokens
                if info.duration_ms is None and duration:
                    info.duration_ms = duration
        if not info.tag:
            info.tag = _short_tag(info.name, "agent")
        if info.duration_ms is None and info.lane_id in first_dt:
            span = last_dt[info.lane_id] - first_dt[info.lane_id]
            millis = int(span.total_seconds() * 1000)
            info.duration_ms = millis or None

    # -- 6b. Unmerged agents: still open, or provably over?
    _settle_open_lanes(lanes, order, spawns)

    # -- 7. Depth, turn and rank.
    def depth_of(lane: str, guard: int = 0) -> int:
        info = lanes.get(lane)
        if info is None or not info.is_branch or guard > len(lanes):
            return 0
        return 1 + depth_of(info.parent_lane, guard + 1)

    def anchor(info: LaneInfo) -> Optional["TemplateMessage"]:
        if info.spawn_index is not None and info.spawn_index in by_index:
            return by_index[info.spawn_index]
        if info.first_index is not None:
            return by_index.get(info.first_index)
        return None

    groups_by_turn: dict[Optional[int], list[LaneInfo]] = {}
    workflow_groups: dict[str, list[LaneInfo]] = {}
    for info in lanes.values():
        node = anchor(info)
        turn = turn_of.get(id(node)) if node is not None else None
        info.turn_index = turn.message_index if turn is not None else None
        if not info.is_branch:
            continue
        info.depth = depth_of(info.lane_id)
        if info.kind == WORKFLOW_AGENT_KIND:
            if not info.group:
                info.group = f"d-{info.spawn_index}"
            workflow_groups.setdefault(info.group, []).append(info)
        else:
            groups_by_turn.setdefault(info.turn_index, []).append(info)

    def rank_key(info: LaneInfo) -> tuple[float, int, float]:
        node = anchor(info)
        spawn_ts = node.meta.timestamp if node is not None and node.meta else None
        return (
            _ts_sort_key(spawn_ts or info.first_ts),
            position.get(id(node), len(order)) if node is not None else len(order),
            _ts_sort_key(info.first_ts),
        )

    for members in groups_by_turn.values():
        for rank, info in enumerate(sorted(members, key=rank_key), start=1):
            info.rank = rank

    # A workflow's agents share their spawn row: rank by when each started,
    # then by the agent card's place (journal order).
    def group_key(info: LaneInfo) -> tuple[float, int]:
        head = by_index.get(info.head_index) if info.head_index is not None else None
        return (
            _ts_sort_key(info.first_ts),
            position.get(id(head), len(order)) if head is not None else len(order),
        )

    for members in workflow_groups.values():
        for rank, info in enumerate(sorted(members, key=group_key), start=1):
            info.rank = rank

    # Spawn (DOM) order for the returned mapping.
    ordered = sorted(
        lanes.values(),
        key=lambda info: (
            position.get(id(anchor(info)), len(order))
            if anchor(info) is not None
            else len(order),
            _ts_sort_key(info.first_ts),
        ),
    )
    model.lanes = {info.lane_id: info for info in ordered}
    return model


def _new_workflow_lane(
    owner: "TemplateMessage",
    spawn: Optional["TemplateMessage"],
    lane_of: dict[int, str],
    lanes: dict[str, LaneInfo],
) -> str:
    """Open the lane of a workflow agent card's transcript; returns its id.

    The spawn row is the card's parent (its phase card, or the Workflow
    call's result for a run without phases); the card itself is the lane's
    head and, once the agent reported, its merge row (step 6).
    """
    content = owner.content
    agent_id = (
        content.agent_id if isinstance(content, WorkflowAgentMessage) else ""
    ) or (str(owner.message_index) if owner.message_index is not None else "x")
    lane = f"{WORKFLOW_LANE_PREFIX}{agent_id}"
    suffix = 2
    while lane in lanes:  # a resumed run may list an agent id again
        lane = f"{WORKFLOW_LANE_PREFIX}{agent_id}-{suffix}"
        suffix += 1
    lanes[lane] = LaneInfo(
        lane_id=lane,
        kind=WORKFLOW_AGENT_KIND,
        name=agent_id,
        parent_lane=lane_of.get(id(owner), MAIN_LANE),
        spawn_index=spawn.message_index if spawn is not None else None,
        head_index=owner.message_index,
        agent_id=agent_id,
    )
    return lane


def _describe_workflow_lane(
    info: LaneInfo, by_index: dict[int, "TemplateMessage"]
) -> None:
    """Step 6 for a workflow agent lane: name, tag, meta, group, merge row,
    accounting and — without a result — whether it is still open."""
    owner = by_index.get(info.head_index) if info.head_index is not None else None
    content = owner.content if owner is not None else None
    if not isinstance(content, WorkflowAgentMessage):
        return
    label = content.label or info.agent_id or info.name
    info.name = label
    # "review:loader" → "loader": the part that tells a phase's agents apart.
    info.tag = _short_tag(label.rsplit(":", 1)[-1], "agent")
    meta_parts = [
        part
        for part in (
            content.phase_title,
            content.model,
            content.state if content.state not in ("", "done", "completed") else "",
        )
        if part
    ]
    info.meta = " · ".join(meta_parts)
    if content.run_id:
        info.group = (
            f"{content.run_id}/{content.phase_ordinal}"
            if content.phase_ordinal is not None
            else content.run_id
        )
    reported = (
        content.result is not None
        or bool(content.result_preview)
        or content.state in ("done", "completed")
    )
    if reported and owner is not None and owner.should_render:
        info.merge_index = owner.message_index
    else:
        info.merge_index = None
        # A finished run (its snapshot exists) or an agent in a terminal
        # state proves the agent is over; else it may still be running.
        over = content.run_finished or (
            content.state.lower() not in _WORKFLOW_RUNNING_STATES
        )
        info.state = "ended" if over else "open"
    if content.tokens:
        info.total_tokens = content.tokens
    if content.duration_ms:
        info.duration_ms = content.duration_ms


def _proves_parent_moved_on(node: "TemplateMessage") -> bool:
    """A card on a parent line that can only appear once a blocking call ended.

    A new model step (text or thinking) or a new prompt: the parent of a
    synchronous agent waits for its tool_result before either can happen.
    Steering messages are typed *while* tools run, so they prove nothing;
    neither do tool calls (a parallel batch), results, notifications or
    hooks.
    """
    content = node.content
    if isinstance(content, UserSteeringMessage):
        return False
    return isinstance(
        content,
        (
            AssistantTextMessage,
            ThinkingMessage,
            UserTextMessage,
            CompactedSummaryMessage,
        ),
    )


def _settle_open_lanes(
    lanes: dict[str, LaneInfo],
    order: list[tuple["TemplateMessage", Optional["TemplateMessage"]]],
    spawns: dict[str, _Spawn],
) -> None:
    """Set ``state`` on every agent lane that has no merge row (step 6b).

    ``"ended"`` when the page proves the agent is over, else ``"open"`` —
    see the module docstring. Deterministic: reads only the tree.
    """
    unmerged = [
        info
        for info in lanes.values()
        if info.is_branch and info.kind != "fork" and info.merge_index is None
    ]
    if not unmerged:
        return

    # Latest "moved on" time per (lane, session), over the cards that can
    # prove it. Per session: a combined page holds every session of the
    # project in one "main" lane, and another session's activity says
    # nothing about this one's call.
    def line_of(node: Optional["TemplateMessage"]) -> tuple[str, str]:
        if node is None:
            return ("", "")
        return (node.lane_id, (node.meta.session_id if node.meta else "") or "")

    latest: dict[tuple[str, str], datetime] = {}
    for node, _parent in order:
        if not node.should_render or not _proves_parent_moved_on(node):
            continue
        parsed = parse_timestamp(node.meta.timestamp if node.meta else None)
        if parsed is None:
            continue
        key = line_of(node)
        seen = latest.get(key)
        if seen is None or parsed > seen:
            latest[key] = parsed
    # Agent ids a TaskStop reported stopped (its result says so; a stop
    # that found nothing — the task had already finished — proves nothing
    # here, and neither does one still waiting for its result).
    stop_target: dict[str, str] = {}
    stopped: set[str] = set()
    for node, _parent in order:
        content = node.content
        if isinstance(content, ToolUseMessage) and isinstance(
            content.input, TaskStopInput
        ):
            if content.input.task_id:
                stop_target[content.tool_use_id] = content.input.task_id
        elif isinstance(content, ToolResultMessage) and isinstance(
            content.output, TaskStopOutput
        ):
            target = stop_target.get(content.tool_use_id)
            if target and content.output.stopped:
                stopped.add(target)

    for info in unmerged:
        if info.kind == WORKFLOW_AGENT_KIND:
            continue  # decided from the run (_describe_workflow_lane)
        info.state = "open"
        if info.agent_id and info.agent_id in stopped:
            info.state = "ended"
            continue
        if info.kind != "agent":
            continue  # async: the parent never waits for it
        rec = spawns.get(info.agent_id or "")
        spawn = rec.spawn if rec is not None else None
        spawned = parse_timestamp(
            spawn.meta.timestamp if spawn is not None and spawn.meta else None
        )
        moved = latest.get(line_of(spawn))
        if spawned is not None and moved is not None and moved > spawned:
            info.state = "ended"

    # A lane nested in one that merged or ended is over too.
    def over(lane_id: str, guard: int = 0) -> bool:
        info = lanes.get(lane_id)
        if info is None or not info.is_branch or info.kind == "fork":
            return False
        if info.merge_index is not None or info.state == "ended":
            return True
        return guard <= len(lanes) and over(info.parent_lane, guard + 1)

    for info in unmerged:
        if info.state == "open" and over(info.parent_lane):
            info.state = "ended"


# ---------------------------------------------------------------------------
# Teammate anchors (§ 1.6.5, § 7 decision 4)
# ---------------------------------------------------------------------------

# The lead's name as teammates address it (``SendMessage`` recipient,
# ``<teammate-message teammate_id=…>`` sender).
TEAM_LEAD = "team-lead"

# message_index → [(target message_index, link label)]
CrossLinks = dict[int, list[tuple[int, str]]]


def _norm_text(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def teammate_links(roots: list["TemplateMessage"], model: LaneModel) -> CrossLinks:
    """Same-page links between the two ends of each teammate exchange.

    Teammate threads are not branches: they stay nested where the renderer
    puts them (under the spawning result) and the minimal theme links them
    up instead —

    - the spawn card → the thread's first card (``→ <name>'s thread``);
    - a ``SendMessage`` card → the ``<teammate-message>`` card that delivered
      it in the recipient's thread (``→ received by <name>``), and that card
      back → the ``SendMessage`` (``← sent by <name>``).

    A message is matched on (sender, recipient, whitespace-normalised body),
    first unused match in DOM order; anything unresolved gets no link rather
    than a dead one. Thread identity comes from the card's
    ``{trunk}#agent-<id>`` session line (a teammate's agent id → its name);
    cards outside any agent thread are the lead's. Only called when the page
    has teammates, so a page without them pays one model check.
    """
    from .models import SendMessageInput, TeammateMessage

    teammates = model.teammates
    links: CrossLinks = {}
    if not teammates:
        return links
    name_of_agent = {t.agent_id: t.name for t in teammates if t.agent_id}
    names = set(name_of_agent.values())

    def add(index: Optional[int], target: Optional[int], label: str) -> None:
        if index is None or target is None or index == target:
            return
        links.setdefault(index, []).append((target, label))

    for t in teammates:
        add(t.spawn_index, t.first_index, f"→ {t.name}'s thread")

    def thread_of(node: "TemplateMessage") -> Optional[str]:
        """The teammate name owning ``node``, ``TEAM_LEAD``, or None."""
        sid = (node.meta.session_id if node.meta else None) or ""
        if "#agent-" not in sid:
            return TEAM_LEAD
        return name_of_agent.get(sid.rsplit("#agent-", 1)[-1])

    # (sender, recipient, body) → card indices, DOM order.
    sent: dict[tuple[str, str, str], list[int]] = {}
    received: dict[tuple[str, str, str], list[int]] = {}
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if not node.should_render or node.message_index is None:
            continue
        content = node.content
        if isinstance(content, ToolUseMessage) and isinstance(
            content.input, SendMessageInput
        ):
            sender = thread_of(node)
            recipient = content.input.recipient or ""
            if sender is None or not recipient or recipient == "*":
                continue
            if recipient not in names:
                recipient = TEAM_LEAD
            key = (sender, recipient, _norm_text(content.input.content))
            sent.setdefault(key, []).append(node.message_index)
        elif isinstance(content, TeammateMessage):
            owner = thread_of(node)
            if owner is None:
                continue
            for block in content.blocks:
                if block.is_system:
                    continue
                sender = block.teammate_id if block.teammate_id in names else TEAM_LEAD
                key = (sender, owner, _norm_text(block.body))
                if node.message_index not in received.get(key, []):
                    received.setdefault(key, []).append(node.message_index)

    for key, senders in sent.items():
        targets = received.get(key, [])
        sender, recipient, _body = key
        for source, target in zip(senders, targets):
            add(source, target, f"→ received by {recipient}")
            add(target, source, f"← sent by {sender}")
    return links

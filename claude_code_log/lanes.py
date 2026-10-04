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
- **Workflow** sub-agents (#174) are not lanes either (§ 7 decision 6):
  everything grafted under a Workflow tool_use inherits its lane.
- **Forks** — at a rewind every child becomes a branch pseudo-session
  (dev-docs/dag.md § 7). The **earliest** branch continues its fork point's
  lane (§ 7 decision 3; its header is recorded in ``continuations``); every
  later one is lane ``branch-<branch sid>``, spawned at the fork-point card
  and never merged. A fork inside a fork lane nests the same way.
- Everything else is ``main``.

A lane exists only once it has a rendered card: a sub-agent whose
transcript deduplicated into its spawn pair (#213), or was stripped at a
reduced detail level, renders as an ordinary tool call.

Each branch lane also carries the **user turn** it belongs to — the
top-level card of the conversation (a direct child of a session or branch
header) that contains its spawn row — and its 1-based **rank** in that
turn by spawn time. The client caps interleaving at three per turn and
puts the rest behind "+N more branches" (§ 1.6.3, § 7 decision 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .models import (
    TaskInput,
    TaskNotificationMessage,
    TaskOutput,
    ToolResultMessage,
    ToolUseMessage,
)
from .utils import compact_count, format_duration, parse_timestamp

if TYPE_CHECKING:
    from .renderer import TemplateMessage

MAIN_LANE = "main"

# Node types grafted under a Workflow tool_use (#174) — never lanes.
_WORKFLOW_TYPES = frozenset({"workflow_phase", "workflow_agent"})


@dataclass
class LaneInfo:
    """One branch lane (or, for ``kind="teammate"``, one teammate thread)."""

    lane_id: str  # "agent-<agentId>" | "branch-<branch sid>"
    kind: str  # "agent" | "async-agent" | "fork" | "teammate"
    name: str  # Task description / teammate name / branch preview
    parent_lane: str = MAIN_LANE  # enclosing lane ("main" or a lane id)
    depth: int = 1  # 1 = opened from main; +1 per enclosing lane
    tag: str = ""  # one short word for the gutter of interleaved rows
    meta: str = ""  # "Explore · claude-sonnet-4 · async" / "rewind"
    spawn_index: Optional[int] = None  # card that opens the lane
    merge_index: Optional[int] = None  # card that closes it (None: forks)
    head_index: Optional[int] = None  # card carrying the lane's description
    first_index: Optional[int] = None  # the lane's first rendered card
    turn_index: Optional[int] = None  # user turn the spawn row belongs to
    rank: int = 0  # 1-based spawn-time order within the turn (0: teammates)
    cards: int = 0  # rendered cards in the lane
    steps: int = 0  # rendered cards, the second half of each pair excluded
    total_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    agent_id: Optional[str] = None  # agents and teammates
    branch_session_id: Optional[str] = None  # forks

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
    for pos, (node, parent) in enumerate(order):
        key = id(node)
        position[key] = pos
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
            lane = inherited
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
        if info.kind != "fork":
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
    for info in lanes.values():
        node = anchor(info)
        turn = turn_of.get(id(node)) if node is not None else None
        info.turn_index = turn.message_index if turn is not None else None
        if not info.is_branch:
            continue
        info.depth = depth_of(info.lane_id)
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

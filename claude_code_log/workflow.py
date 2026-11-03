"""Parse Claude Code *dynamic Workflow* runs (issue #174, PR1 — parse only).

A Workflow tool_use launches an orchestrator that fans out into many
side-channel sub-agents. On disk (see ``work/dynamic-workflow-support.md``
§1) a run under a trunk session ``<sid>/`` leaves:

    <sid>/subagents/workflows/<runId>/
        journal.jsonl                 live spine: started/result events, keyed by agentId
        agent-<agentId>.jsonl         per-agent side-channel transcript
        agent-<agentId>.meta.json     {"agentType": "workflow-subagent"}
    <sid>/workflows/<runId>.json      terminal snapshot: phases + per-agent metadata
                                      + the script that ran (+ args, summary,
                                      error, run totals)

This module turns that into a :class:`WorkflowRun`. Strategy (D1):
journal-led, ``<runId>.json``-enriched — ``journal.jsonl`` is the
authoritative live spine (present from the start, carries full results,
keyed by ``agentId``); ``<runId>.json`` is *optional* enrichment present
only after completion (phases + tokens/state/model per agent). A running
workflow with no snapshot still parses: agents in journal order, no phase
grouping.

This module does **no rendering** — wiring runs into the message tree is
a later phase. ``load_transcript`` is imported lazily to avoid a circular
import with ``converter``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

logger = logging.getLogger(__name__)

_WF_META_RE = re.compile(
    r"export\s+(?:const|let|var)\s+meta\s*=\s*\{(.*?)\n\}", re.DOTALL
)
# A JS string literal in any of the three quote styles, with backslash-escape
# support — real meta descriptions contain escaped quotes ('SAM\'s ...'),
# which a naive [^'\"]* would truncate at.
_WF_JS_STRING = (
    r"(?:'((?:[^'\\]|\\.)*)'"
    r'|"((?:[^"\\]|\\.)*)"'
    r"|`((?:[^`\\]|\\.)*)`)"
)
_WF_NAME_RE = re.compile(r"\bname\s*:\s*" + _WF_JS_STRING)
_WF_DESC_RE = re.compile(r"\bdescription\s*:\s*" + _WF_JS_STRING)
_WF_PHASES_KEY_RE = re.compile(r"\bphases\s*:")
_WF_TITLE_RE = re.compile(r"title\s*:\s*" + _WF_JS_STRING)


def _js_string_value(m: "re.Match[str]") -> str:
    """Extract + unescape the string from a ``_WF_JS_STRING`` match (exactly
    one of its three alternation groups is non-None). Unescaping is the
    best-effort ``\\x`` → ``x`` — right for the quotes/backslashes that occur
    in display strings, and never worse than the truncation it replaces."""
    value = next((g for g in m.groups()[-3:] if g is not None), "")
    return re.sub(r"\\(.)", r"\1", value)


def parse_workflow_meta(script: str) -> tuple[str, str, list[str]]:
    """Best-effort ``(name, description, phase_titles)`` from a Workflow
    script's ``export const meta = {...}`` block — a display aid shared by
    the HTML and Markdown renderers (issue #174).

    Matches only the **exported** declaration the Workflow tool mandates —
    ``export const meta = { ... \\n}`` (also ``let``/``var``) — so an
    unrelated local ``meta = {...}`` elsewhere in the orchestrator can't be
    mis-parsed as the header. The closing-brace anchor is at column 0 (an
    indented close yields no header). All field lookups are scoped to the
    matched block, so they can't pick up ``name:``/``title:`` elsewhere in
    the body. Phase titles are collected from every ``title:`` *after* the
    ``phases:`` key, so a ``depth`` string containing ``]`` doesn't truncate
    the list. String values may use any JS quote style (``'``/``"``/backtick)
    and may contain backslash-escaped quotes (``'SAM\\'s sweep'`` — observed
    in real meta blocks). Returns empty values when the block or a field
    isn't found.
    """
    block_m = _WF_META_RE.search(script)
    if not block_m:
        return "", "", []
    block = block_m.group(1)
    name_m = _WF_NAME_RE.search(block)
    desc_m = _WF_DESC_RE.search(block)
    phases: list[str] = []
    phases_key = _WF_PHASES_KEY_RE.search(block)
    if phases_key:
        phases = [
            title
            for m in _WF_TITLE_RE.finditer(block[phases_key.end() :])
            if (title := _js_string_value(m))
        ]
    return (
        _js_string_value(name_m) if name_m else "",
        _js_string_value(desc_m) if desc_m else "",
        phases,
    )


def resolve_workflow_script(run: "Optional[WorkflowRun]", script: str) -> str:
    """Resolve the *effective* orchestrator source for a Workflow tool_use.

    A Workflow invocation carries the script inline only in the ``script``
    input shape; the ``scriptPath`` / ``name`` / ``resumeFromRunId`` shapes
    carry no source at all. The terminal ``<runId>.json`` snapshot, however,
    stores the full text that actually ran (its ``script`` field) — so fall
    back to that. Empty when neither is available (e.g. a still-running
    ``scriptPath`` invocation with no snapshot yet).
    """
    if script:
        return script
    if run is not None:
        return run.script
    return ""


def resolve_workflow_header(
    run: "Optional[WorkflowRun]", script: str
) -> tuple[str, str, list[str]]:
    """Resolve ``(name, description, phase_titles)`` for the Workflow header,
    **snapshot-first** (issue #174 PR3 / cboos's refinement).

    Prefers the authoritative ``<runId>.json`` (``run.workflow_name`` and
    ``run.phases`` titles) when a snapshot is present, effectively *back-filling*
    the header from the JSON. Falls back to the best-effort JS-``meta`` regex
    (:func:`parse_workflow_meta`) for a running workflow with no snapshot.

    ``script`` is the tool_use's inline source, empty for the ``scriptPath`` /
    ``name`` invocation shapes — the meta parse then runs on the snapshot's
    stored ``script`` (:func:`resolve_workflow_script`), which is where
    ``description`` usually comes from for those shapes. When even that yields
    no description, the snapshot's ``summary`` (the tool's own digest of the
    description) fills in.

    When a snapshot IS present and a *non-empty* script failed the JS-``meta``
    parse for a field the snapshot supplies, emit a warning so genuine
    JS-format drift stays noticeable (we can then adapt the regex). No script
    text at all is NOT drift — no warning.
    """
    effective_script = resolve_workflow_script(run, script)
    name_js, description, phases_js = parse_workflow_meta(effective_script)

    if run is not None and getattr(run, "has_snapshot", False):
        if not description:
            description = run.summary
        if effective_script:
            if run.workflow_name and not name_js:
                logger.warning(
                    "Workflow meta: JS `name` not parsed but snapshot has "
                    "workflowName=%r — the script's `meta` format may have drifted.",
                    run.workflow_name,
                )
            if run.phases and not phases_js:
                logger.warning(
                    "Workflow meta: JS `phases` not parsed but snapshot has %d "
                    "phase(s) — the script's `meta` format may have drifted.",
                    len(run.phases),
                )
        name = run.workflow_name or name_js
        phase_titles = [p.title for p in run.phases] or phases_js
        return name, description, phase_titles

    return name_js, description, phases_js


if TYPE_CHECKING:
    from claude_code_log.models import TranscriptEntry


@dataclass
class WorkflowAgent:
    """One sub-agent of a workflow run.

    ``result`` is the agent's full output from the journal (a dict for
    ``StructuredOutput`` agents, a string for plain-text agents, or
    ``None`` if the run is still in flight). Phase/metadata fields are
    populated only when the ``<runId>.json`` snapshot is present.
    """

    agent_id: str
    label: str = ""
    phase_index: Optional[int] = None
    phase_title: str = ""
    model: str = ""
    state: str = ""
    tokens: Optional[int] = None
    tool_calls: Optional[int] = None
    duration_ms: Optional[int] = None
    attempt: Optional[int] = None
    result: Any = None
    result_preview: str = ""
    entries: list["TranscriptEntry"] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


@dataclass
class WorkflowPhase:
    """A phase grouping of agents (only built when the snapshot is present)."""

    index: int
    title: str
    depth: str = ""
    agents: list[WorkflowAgent] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


@dataclass
class WorkflowRun:
    """A parsed dynamic-workflow run.

    ``agents`` is the flat list in journal (launch) order — always present.
    ``phases`` is populated only when ``<runId>.json`` was found
    (``has_snapshot``); each phase references the same WorkflowAgent objects
    as ``agents``. ``result`` is the run's final answer (snapshot ``result``).

    ``agent_count`` is the snapshot's self-reported ``agentCount`` and may be
    LESS than ``len(agents)``: the journal lists every launched agent, while
    the snapshot counts only those that produced a result (retried/abandoned
    agents appear in ``agents`` but not in ``agent_count`` — e.g. 42 vs 40 on
    the §1 reference run).

    ``script`` is the snapshot's stored copy of the orchestrator source that
    actually ran — the recovery route for the ``scriptPath`` / ``name``
    invocation shapes, whose tool_use input carries no source. ``script_path``
    is where that source lived (the caller's file for a ``scriptPath``
    invocation, the session-dir persisted copy for an inline one). ``summary``
    is the tool's digest of the meta description; ``error`` is set for a
    ``failed`` run (possibly one that died before launching any agent — such a
    run has a snapshot but no journal, so ``agents`` is empty).
    """

    run_id: str
    task_id: str = ""
    workflow_name: str = ""
    status: str = ""
    phases: list[WorkflowPhase] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    agents: list[WorkflowAgent] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    result: Any = None
    total_tokens: Optional[int] = None
    agent_count: Optional[int] = None
    has_snapshot: bool = False
    script: str = ""
    script_path: str = ""
    args: Any = None
    summary: str = ""
    error: str = ""
    default_model: str = ""
    duration_ms: Optional[int] = None
    total_tool_calls: Optional[int] = None


def _read_jsonl(path: Path) -> list[Any]:
    """Read a JSONL file into a list of parsed values (skip blank/bad lines)."""
    rows: list[Any] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _as_int(value: Any) -> Optional[int]:
    """Coerce a snapshot value to ``int`` defensively, else ``None``.

    Snapshot fields come straight from JSON and a variant/malformed payload
    could carry a numeric value as a string (``"phaseIndex": "1"``) or float.
    Normalising here keeps every numeric field a real ``int | None`` so
    downstream numeric comparisons (e.g. phase-index range checks) can never
    raise ``TypeError`` and crash the whole run parse. ``bool`` is rejected
    (it's an ``int`` subclass but never a meaningful count/index).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str:
    """Coerce a snapshot value to ``str`` defensively, else ``""``.

    Same posture as :func:`_as_int`: snapshot fields come straight from JSON,
    so a variant payload could carry a non-string where we expect text.
    """
    return value if isinstance(value, str) else ""


def _parse_journal(path: Path) -> tuple[list[str], dict[str, Any]]:
    """Parse ``journal.jsonl`` into (agent order, {agentId: result}).

    Order is by first appearance (``started`` preferred, else ``result``).
    The last ``result`` for an agent wins (covers retries/attempts).
    """
    order: list[str] = []
    seen: set[str] = set()
    results: dict[str, Any] = {}
    for raw_row in _read_jsonl(path):
        if not isinstance(raw_row, dict):
            continue
        row = cast("dict[str, Any]", raw_row)
        agent_id = row.get("agentId")
        if not isinstance(agent_id, str):
            continue
        if agent_id not in seen:
            seen.add(agent_id)
            order.append(agent_id)
        if row.get("type") == "result":
            results[agent_id] = row.get("result")
    return order, results


def _load_snapshot(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load ``<runId>.json`` → (raw, phases[], {agentId: agent-progress-node}).

    Returns empty structures when the file is missing or unparseable, so
    callers can treat the snapshot as purely optional enrichment.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}, [], {}
    if not isinstance(loaded, dict):
        return {}, [], {}
    raw = cast("dict[str, Any]", loaded)

    raw_phases = raw.get("phases")
    phases: list[dict[str, Any]] = []
    if isinstance(raw_phases, list):
        phases = [
            cast("dict[str, Any]", p)
            for p in cast("list[Any]", raw_phases)
            if isinstance(p, dict)
        ]

    agent_meta: dict[str, dict[str, Any]] = {}
    raw_progress = raw.get("workflowProgress")
    if isinstance(raw_progress, list):
        for raw_node in cast("list[Any]", raw_progress):
            if not isinstance(raw_node, dict):
                continue
            node = cast("dict[str, Any]", raw_node)
            if node.get("type") != "workflow_agent":
                continue
            aid = node.get("agentId")
            if isinstance(aid, str):
                agent_meta[aid] = node
    return raw, phases, agent_meta


def parse_workflow_run(
    run_dir: Path,
    snapshot_path: Optional[Path] = None,
    *,
    silent: bool = True,
) -> Optional[WorkflowRun]:
    """Parse one workflow run from its ``subagents/workflows/<runId>/`` dir.

    ``snapshot_path`` is the optional ``<runId>.json`` terminal snapshot.
    A run that failed before launching any agent leaves a snapshot but NO
    run dir/journal — that still parses (as a snapshot-only run with an
    empty ``agents`` list), so the failure stays visible at the tool_use.
    Returns ``None`` when there is neither a journal nor a snapshot (not a
    workflow run).
    """
    journal = run_dir / "journal.jsonl"
    has_journal = journal.is_file()
    order: list[str] = []
    results: dict[str, Any] = {}
    if has_journal:
        order, results = _parse_journal(journal)
    elif snapshot_path is None or not snapshot_path.is_file():
        return None

    run_id = run_dir.name
    task_id = workflow_name = status = ""
    total_tokens: Optional[int] = None
    agent_count: Optional[int] = None
    run_result: Any = None
    phases_meta: list[dict[str, Any]] = []
    agent_meta: dict[str, dict[str, Any]] = {}
    has_snapshot = False
    script = script_path = summary = error = default_model = ""
    run_args: Any = None
    duration_ms: Optional[int] = None
    total_tool_calls: Optional[int] = None

    if snapshot_path is not None and snapshot_path.is_file():
        raw, phases_meta, agent_meta = _load_snapshot(snapshot_path)
        if raw:
            has_snapshot = True
            run_id = raw.get("runId") or run_id
            task_id = raw.get("taskId") or ""
            workflow_name = raw.get("workflowName") or ""
            status = raw.get("status") or ""
            total_tokens = _as_int(raw.get("totalTokens"))
            agent_count = _as_int(raw.get("agentCount"))
            run_result = raw.get("result")
            script = _as_str(raw.get("script"))
            script_path = _as_str(raw.get("scriptPath"))
            run_args = raw.get("args")
            summary = _as_str(raw.get("summary"))
            error = _as_str(raw.get("error"))
            default_model = _as_str(raw.get("defaultModel"))
            duration_ms = _as_int(raw.get("durationMs"))
            total_tool_calls = _as_int(raw.get("totalToolCalls"))

    if not has_journal and not has_snapshot:
        # A snapshot file that failed to load — nothing to build a run from.
        return None

    # Union of journal order with any snapshot-only agent ids (defensive).
    all_ids = list(order)
    for aid in agent_meta:
        if aid not in all_ids:
            all_ids.append(aid)

    # Lazy import avoids a circular dependency with converter.
    from claude_code_log.converter import load_transcript

    agents: list[WorkflowAgent] = []
    for aid in all_ids:
        meta = agent_meta.get(aid, {})
        agent_file = run_dir / f"agent-{aid}.jsonl"
        entries: list[Any] = []
        if agent_file.is_file():
            entries = load_transcript(agent_file, silent=silent)
        agents.append(
            WorkflowAgent(
                agent_id=aid,
                label=meta.get("label") or "",
                phase_index=_as_int(meta.get("phaseIndex")),
                phase_title=meta.get("phaseTitle") or "",
                model=meta.get("model") or "",
                state=meta.get("state") or "",
                tokens=_as_int(meta.get("tokens")),
                tool_calls=_as_int(meta.get("toolCalls")),
                duration_ms=_as_int(meta.get("durationMs")),
                attempt=_as_int(meta.get("attempt")),
                result=results.get(aid),
                result_preview=meta.get("resultPreview") or "",
                entries=entries,
            )
        )

    phases = _group_into_phases(phases_meta, agents)

    return WorkflowRun(
        run_id=run_id,
        task_id=task_id,
        workflow_name=workflow_name,
        status=status,
        phases=phases,
        agents=agents,
        result=run_result,
        total_tokens=total_tokens,
        agent_count=agent_count,
        has_snapshot=has_snapshot,
        script=script,
        script_path=script_path,
        args=run_args,
        summary=summary,
        error=error,
        default_model=default_model,
        duration_ms=duration_ms,
        total_tool_calls=total_tool_calls,
    )


def _group_into_phases(
    phases_meta: list[dict[str, Any]], agents: list[WorkflowAgent]
) -> list[WorkflowPhase]:
    """Build phases from snapshot ``phases[]`` and assign agents to them.

    Agents map to a phase by ``phase_title`` (authoritative), falling back
    to ``phase_index``. Title is preferred because the two carry *different
    index bases* in real data: the ``phases[]`` array is 0-based, but each
    agent's ``phaseIndex`` (and the ``workflow_phase`` node ``index``) is
    1-based — so indexing ``phases[phase_index]`` directly shifts every
    agent one phase over. Titles ("Map"/"Verify"/...) match across both and
    sidestep the offset entirely. ``phase_index`` is used only when the
    title is missing/unmatched (treated as 0-based, best effort).

    Returns ``[]`` when there is no snapshot — the WIP/journal-only view
    groups agents only as the flat ``agents`` list.
    """
    if not phases_meta:
        return []
    phases = [
        WorkflowPhase(
            index=idx, title=pm.get("title") or "", depth=pm.get("depth") or ""
        )
        for idx, pm in enumerate(phases_meta)
    ]
    title_to_idx = {p.title: p.index for p in phases if p.title}
    for agent in agents:
        idx: Optional[int] = None
        if agent.phase_title and agent.phase_title in title_to_idx:
            idx = title_to_idx[agent.phase_title]
        elif agent.phase_index is not None and 0 <= agent.phase_index < len(phases):
            idx = agent.phase_index
        if idx is not None and 0 <= idx < len(phases):
            phases[idx].agents.append(agent)
    return phases


def discover_workflow_runs(session_dir: Path) -> list[tuple[Path, Optional[Path]]]:
    """Find ``(run_dir, snapshot_path)`` pairs under one trunk session dir.

    ``run_dir`` is ``<session_dir>/subagents/workflows/<runId>/`` (must
    contain ``journal.jsonl``); ``snapshot_path`` is the matching
    ``<session_dir>/workflows/<runId>.json`` if present, else ``None``.

    Also surfaces *snapshot-only* runs: a workflow that died before
    launching any agent (e.g. a script error on the first line) leaves a
    ``<session_dir>/workflows/<runId>.json`` but no run dir at all. These
    yield a (non-existent) ``run_dir`` path alongside their snapshot, which
    :func:`parse_workflow_run` accepts.
    """
    base = session_dir / "subagents" / "workflows"
    snapshots_dir = session_dir / "workflows"
    runs: list[tuple[Path, Optional[Path]]] = []
    seen: set[str] = set()
    if base.is_dir():
        for run_dir in sorted(base.iterdir()):
            if not run_dir.is_dir() or not (run_dir / "journal.jsonl").is_file():
                continue
            seen.add(run_dir.name)
            snapshot = snapshots_dir / f"{run_dir.name}.json"
            runs.append((run_dir, snapshot if snapshot.is_file() else None))
    if snapshots_dir.is_dir():
        for snapshot in sorted(snapshots_dir.glob("*.json")):
            run_id = snapshot.stem
            if run_id not in seen:
                runs.append((base / run_id, snapshot))
    return runs


def _runs_in_session_dir(
    session_dir: Path, *, silent: bool = True
) -> list[WorkflowRun]:
    """Parse every workflow run under one trunk session dir's
    ``subagents/workflows/``. Shared by the directory and single-file loaders."""
    runs: list[WorkflowRun] = []
    for run_dir, snapshot in discover_workflow_runs(session_dir):
        parsed = parse_workflow_run(run_dir, snapshot, silent=silent)
        if parsed is not None:
            runs.append(parsed)
    return runs


def load_workflow_runs(
    directory_path: Path, *, silent: bool = True
) -> list[WorkflowRun]:
    """Discover and parse every workflow run under a project directory.

    Each trunk ``<session>.jsonl`` has a sibling ``<session>/`` dir whose
    ``subagents/workflows/<runId>/`` subtrees are the runs. Parse-only —
    the caller decides what to do with the returned runs.
    """
    runs: list[WorkflowRun] = []
    for session_dir in sorted(p for p in directory_path.iterdir() if p.is_dir()):
        runs.extend(_runs_in_session_dir(session_dir, silent=silent))
    return runs


def load_session_workflow_runs(
    transcript_path: Path, *, silent: bool = True
) -> list[WorkflowRun]:
    """Discover + parse workflow runs for a SINGLE session transcript file
    (issue #174 PR3 — single-file rendering).

    The runs live in the sibling ``<SID>/subagents/workflows/<runId>/`` dir
    (snapshot ``<SID>/workflows/<runId>.json``), where ``<SID>`` is the
    transcript filename without its ``.jsonl`` suffix. Mirrors
    :func:`load_workflow_runs` scoped to one session, so
    ``claude-code-log <project>/<SID>.jsonl`` renders the workflow tree just
    like a directory load.
    """
    return _runs_in_session_dir(transcript_path.with_suffix(""), silent=silent)


def _tool_result_text(content: Any) -> str:
    """Best-effort plain text from a raw ``ToolResultContent.content`` (a
    string, or a list of ``{type, text, ...}`` dicts) for ``Task ID:`` lookup."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in cast("list[Any]", content):
            if isinstance(item, dict):
                text = cast("dict[str, Any]", item).get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def map_workflow_runs_by_tool_use(
    entries: "list[TranscriptEntry]", runs: "list[WorkflowRun]"
) -> dict[str, WorkflowRun]:
    """Resolve ``{Workflow tool_use_id: WorkflowRun}`` at full-session scope.

    Matches each ``Workflow`` tool_use to its run via the paired tool_result's
    ``Task ID: <taskId>`` (the ``runId`` lives only in the dropped
    ``toolUseResult``) == ``run.task_id``. Built over the WHOLE entry list
    *before* pagination splits it into pages, so a tool_use and its tool_result
    on different pages still link (the per-page linker alone would miss it).
    Also the linkage used for single-file rendering.
    """
    from .models import ToolResultContent, ToolUseContent

    runs_by_task = {r.task_id: r for r in runs if r.task_id}
    if not runs_by_task:
        return {}

    workflow_tool_use_ids: set[str] = set()
    for entry in entries:
        content = getattr(getattr(entry, "message", None), "content", None)
        if not isinstance(content, list):
            continue
        for item in cast("list[Any]", content):
            if isinstance(item, ToolUseContent) and item.name == "Workflow":
                workflow_tool_use_ids.add(item.id)
    if not workflow_tool_use_ids:
        return {}

    links: dict[str, WorkflowRun] = {}
    for entry in entries:
        content = getattr(getattr(entry, "message", None), "content", None)
        if not isinstance(content, list):
            continue
        for item in cast("list[Any]", content):
            if (
                isinstance(item, ToolResultContent)
                and item.tool_use_id in workflow_tool_use_ids
            ):
                match = _WF_TASK_ID_RE_RUNTIME.search(_tool_result_text(item.content))
                if match:
                    run = runs_by_task.get(match.group(1))
                    if run is not None:
                        links[item.tool_use_id] = run
    return links


_WF_TASK_ID_RE_RUNTIME = re.compile(r"Task ID:\s*(\S+)")

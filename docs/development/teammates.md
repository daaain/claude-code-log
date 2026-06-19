# Teammates Support

> See [application_model.md](application_model.md) for the system overview.

This document describes how `claude-code-log` supports the Claude Code
teammates feature (research preview, gated by
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, available in CC 2.1.32+).

It is the as-built reference for the work delivered across three PRs:

- **PR #117** (`dev/teammates-parsing`): typed models, parsers, subagent
  linking. No new rendering.
- **PR #122** (`dev/teammates-rendering`): HTML + Markdown formatters,
  CSS, color propagation through `RenderingContext`, snapshot + browser
  tests.
- **PR #125** (`dev/teammates-stitching`): session-header team badge,
  project-index "Team:" annotation. **Followed by a substantial
  in-branch refactor** (commits `fd993f2`, `27e43fb`, `fdd28ec`,
  `7c364bc`, `47bc50e`) prompted by real-world fixture testing —
  registers the `Agent` spawn tool, drops the subagent-rendering
  ceremony (`<details>` collapse, teammate badges on subagent headers),
  splices subagent threads under their trunk anchors via a new
  relocation pass, and compacts `TaskCreate` / `TaskUpdate` /
  `SendMessage` rendering.

This doc captures *what was actually built* — the as-built shape after
the trilogy + the post-merge refactor described above. The companion
DAG architecture is in [`dev-docs/dag.md`](dag.md); message-type
reference is in [`dev-docs/messages.md`](messages.md); the broader
agent-spawning context (sync sub-agents, async task agents, and how
teammates fit in) is in [`dev-docs/agents.md`](agents.md).

Parent issue: [#91 Support teammates](https://github.com/daaain/claude-code-log/issues/91).

---

## 1. Scope and shape of the data

The teammates feature adds three new transcript shapes on top of the
regular Claude Code data model:

### 1.1 Six new tools

The team-lead session uses six new tool names that didn't exist before
2.1.32:

| Tool | Direction | Purpose |
|---|---|---|
| `TeamCreate` | call → JSON result | Create a team, set the lead-agent identity |
| `TeamDelete` | call → JSON result | Tear down a team (refuses if members are still active) |
| `TaskCreate` | call → text result | Add a task to the team's task board |
| `TaskUpdate` | call → text result | Update task status, owner, or fields |
| `TaskList` | call → text result | Read the current task board |
| `SendMessage` | call → JSON result | Send a directed message from lead → teammate |

`Task` itself isn't new, but it gains five teammate-spawn fields when the
team-lead invokes it: `team_name`, `name` (the teammate name),
`mode`, `run_in_background`, and the existing `subagent_type`.

In real teammate transcripts the spawn tool is actually emitted under
the name **`Agent`**, not `Task`. The `tool_factory` aliases `Agent →
TaskInput` / `parse_task_output` (PR3 commit `fd993f2`); Pydantic's
`extra="ignore"` accepts the `isolation` field that `Agent` carries on
top of TaskInput. The `_cleanup_sidechain_duplicates` pass (§4) is
likewise broadened to a `{"Task", "Agent"}` set so dedup fires for both.

### 1.2 `<teammate-message>` blocks in user entries

Teammates send messages back to the lead as user entries whose
`message.content` is a string carrying one or more XML blocks:

```xml
<teammate-message teammate_id="alice" color="blue" summary="relay tests complete">
Relay module coverage is now **96%**. Here's the breakdown:
- 10 tests for `deliver_to_remote`
- 4 tests for `calculate_next_retry`
</teammate-message>
```

Multiple blocks may appear in a single entry, and they may come from
different teammates intermingled. The pseudo-id `teammate_id="system"`
marks system notifications (e.g. `teammate_terminated: alice exited
cleanly`).

### 1.3 `teamName` on every entry

While a team is active, every transcript entry — main session, sidechain,
tool_use, tool_result, system — carries a top-level `teamName` field.
Same value for the duration of the team's activity; first-sighting-wins
when collecting per-session.

### 1.4 The linking problem

Teammate subagent transcripts live at:

```
~/.claude/projects/<project>/<session-id>/subagents/agent-<agent_id>.jsonl
```

These files chain internally via `parentUuid` → `uuid`, but **the first
entry has `parentUuid: null` and no top-level `agentId` field referencing
the spawning Task tool_use**. The link from the main session's `Task`
tool_use to the subagent's session must be reconstructed.

Two pathways below (§4) handle this — one structured, one heuristic.

---

## 2. Data model layer (PR #117)

All new types are additive on top of existing `models.py`. Existing
fields are unchanged; non-teammate transcripts parse identically.

### 2.1 Tool input/output models

Six new `BaseModel`s for the new tool inputs (all use
`model_config = {"extra": "allow"}` so unknown fields don't break
parsing):

```python
TeamCreateInput     team_name, description, agent_type
TeamDeleteInput     team_name (often empty)
TaskCreateInput     subject, description, activeForm
TaskUpdateInput     taskId, owner, status
TaskListInput       (empty)
SendMessageInput    type, recipient, content
```

And six matching output dataclasses:

```python
TeamCreateOutput    team_name, team_file_path, lead_agent_id
TeamDeleteOutput    success, message, team_name, active_members
TaskCreateOutput    task_id, subject
TaskUpdateOutput    success, task_id, updated_fields, status_change
TaskListOutput      tasks: list[TaskListItem]
SendMessageOutput   success, message, request_id, target
```

`TaskListItem` carries `id, subject, status, owner, blocked_by`.
`TaskStatusChange` carries `from_status, to_status`.

The existing `TaskInput` gains five fields for teammate-spawned Tasks:
`team_name`, `name`, `mode` (plus the existing `run_in_background` and
`subagent_type`). `TaskOutput` gains `metadata: Optional[AgentResultMetadata]`,
`teammate_id`, `agent_id`, `color`.

All new models are added to the `ToolInput` / `ToolOutput` unions.

### 2.2 `TeammateMessage` content

The `<teammate-message>` XML blocks parse into:

```python
@dataclass
class TeammateMessageBlock:
    teammate_id: str
    body: str
    color: Optional[str] = None
    summary: Optional[str] = None
    is_system: bool = False  # teammate_id == "system"

@dataclass
class TeammateMessage(MessageContent):
    blocks: list[TeammateMessageBlock]
    leading_text: Optional[str] = None    # text before the first block
    trailing_text: Optional[str] = None   # text after the last block
    # message_type returns "teammate"
```

A single user entry → a single `TeammateMessage` content carrying all its
blocks plus surrounding text. The renderer iterates `blocks` to produce
per-block cards.

### 2.3 `AgentResultMetadata`

Teammate-spawned Tasks (and async-task agents — issue #90) embed a
metadata block at the end of the agent's response:

```
agentId: a4ca7529859c158c2 (use SendMessage with to: '...' to continue this agent)
worktreePath: /.../worktrees/agent-a4ca7529
worktreeBranch: worktree-agent-a4ca7529
<usage>total_tokens: 48421
tool_uses: 24
duration_ms: 802753</usage>
```

Parsed into:

```python
@dataclass
class AgentResultMetadata:
    agent_id: Optional[str]
    worktree_path: Optional[str]
    worktree_branch: Optional[str]
    total_tokens: Optional[int]
    tool_uses: Optional[int]
    duration_ms: Optional[int]
```

Stored on `TaskOutput.metadata`. The text body is stripped of the
metadata tail so the rendered response stays clean.

### 2.4 Transcript / meta extensions

```python
class BaseTranscriptEntry(BaseModel):
    ...
    teamName: Optional[str] = None  # carried verbatim from JSONL

@dataclass
class MessageMeta:
    ...
    team_name: Optional[str] = None
```

`MessageMeta` propagation happens in
[`factories/meta_factory.py`](../claude_code_log/factories/meta_factory.py)
via `getattr(transcript, "teamName", None)` (defensive against older
transcripts).

### 2.5 SessionHeaderMessage extensions (PR #125)

```python
class SessionHeaderMessage(MessageContent):
    ...
    team_name: Optional[str] = None         # set when teamName seen in session
```

This powers the `👥 Team:` badge described in §6. The `teammate_id`,
`teammate_color`, and `collapsed_by_default` fields landed in early
PR #125 commits but were dropped by the post-merge refactor (commit
`27e43fb`); see §4.3 for the rationale.

---

## 3. Parsing layer

### 3.1 `factories/agent_metadata_factory.py`

`parse_agent_result_metadata(text) -> (body, Optional[AgentResultMetadata])`
extracts the metadata tail. Anchored on the first `agentId:` line (or
the `<usage>` block alone for older transcripts that omit `agentId:`),
returns the body with the tail stripped plus the parsed metadata.

Wired into `parse_task_output` so every Task tool_result automatically
gets `metadata` populated when the tail is present.

### 3.2 `factories/teammate_factory.py`

Three exported helpers:

- `has_teammate_message(text) -> bool` — cheap detector (substring +
  regex search).
- `iter_teammate_blocks(text) -> Iterable[TeammateMessageBlock]` — yields
  one `TeammateMessageBlock` per `<teammate-message>` block.
- `create_teammate_message(meta, text) -> Optional[TeammateMessage]` —
  the high-level factory. Returns `None` when no block is present so the
  caller can fall back to default user-text rendering.
- `find_team_lead_body(text) -> Optional[str]` — returns the body of the
  first `<teammate-message teammate_id="team-lead">` block. Used by the
  prompt-hash linking fallback (§4.2).

XML parsing is regex-based with a hand-rolled attribute splitter
(`re.DOTALL` on the block, double-or-single-quoted attrs). No real XML
parser is needed — the bodies routinely contain Markdown and other
XML-looking text that an XML parser would balk on.

`create_user_message` in
[`factories/user_factory.py`](../claude_code_log/factories/user_factory.py)
hooks into the dispatch BEFORE the default text path: if
`has_teammate_message(text)` returns True and `create_teammate_message`
yields a result, that wins.

### 3.3 `factories/tool_factory.py`

The six new tools are registered, plus the `Agent` alias for `Task`
(post-merge fix `fd993f2`):

```python
TOOL_INPUT_MODELS = {
    ...
    "Task":        TaskInput,
    "Agent":       TaskInput,                 # teammates spawn tool alias
    "TeamCreate":  TeamCreateInput,
    "TeamDelete":  TeamDeleteInput,
    "TaskCreate":  TaskCreateInput,
    "TaskUpdate":  TaskUpdateInput,
    "TaskList":    TaskListInput,
    "SendMessage": SendMessageInput,
}

TOOL_OUTPUT_PARSERS = {
    ...
    "Task":        parse_task_output,
    "Agent":       parse_task_output,         # same parse shape as Task
    "TeamCreate":  parse_teamcreate_output,    # JSON
    "TeamDelete":  parse_teamdelete_output,    # JSON + extract active members
    "TaskCreate":  parse_taskcreate_output,    # regex on plain text
    "TaskUpdate":  parse_taskupdate_output,    # regex on plain text
    "TaskList":    parse_tasklist_output,      # one-line-per-task regex
    "SendMessage": parse_sendmessage_output,   # JSON
}
```

`TeamDelete`'s active-member extraction parses the cleanup-failure message
(`"Cannot cleanup team with N active member(s): alice, bob..."`) so the
renderer can surface those names as colored badges.

`TaskList`'s parser bails on any unrecognized line (returns `None`) so
the generic renderer keeps the full text rather than partially mangling
it.

---

## 4. Subagent linking (`converter.py`)

The "linking problem" from §1.4 is resolved by two pathways, tried in
order. Once a Task tool_result has a known `agentId`, the existing
`_integrate_agent_entries` machinery (per
[`dev-docs/dag.md`](dag.md#sessions-and-dag-lines)) takes over:

- subagent entries get a synthetic sessionId `{main}#agent-{agentId}`,
- the subagent's first entry's `parentUuid` is rewritten to the
  spawning Task tool_use uuid,
- the subagent DAG-line attaches as a child session of the main session.

### 4.1 Primary path: `agentId` from the tool_result

When `_integrate_agent_entries` runs, each Task tool_result has either:

- `toolUseResult.agentId` — present in newer transcripts as a top-level
  field on the result, OR
- `TaskOutput.metadata.agent_id` — parsed from the Markdown tail by
  `agent_metadata_factory` (§3.1).

Either suffices for the structural link. This is the happy path for
real-world Anthropic-generated transcripts.

### 4.2 Fallback path: prompt-hash matching

`_link_subagents_by_prompt_hash` runs after the primary path collected
known agent ids. It scans `<session>/subagents/agent-*.jsonl` files
whose stem isn't in the agent-id set, and for each:

1. Read the first entry's text content.
2. If that text contains a `<teammate-message teammate_id="team-lead">`
   block (the canonical teammate-spawn shape), extract the body via
   `find_team_lead_body`. Otherwise use the raw text.
3. Normalize via `_normalize_prompt`: collapse whitespace, lowercase.
4. Compare against each unresolved Task tool_use's `prompt` input
   (similarly normalized). Exact match wins.
5. Back-patch the Task tool_result's `agentId` field, add to the agent-id
   set, and **remove the matched entry from the unresolved pool** so a
   second candidate file with the same prompt can't claim it (this last
   step was a CodeRabbit-driven fix on PR #117 — see commit `cc9951d`).

Pre-normalized prompts are computed once up front to avoid quadratic
work in the inner loop.

### 4.3 Synthetic session IDs and the relocation pass

The synthetic `{main}#agent-{agentId}` sessionId is **kept** (the
teammate-name variant `{main}#teammate-{name}@{team}` from the plan was
never adopted). Without the rewrite, every subagent's sidechain entries
share the trunk's `sessionId` — `_walk_session_with_forks` then folds
them into the trunk's DAG-line and `_collect_agent_anchors` scoops them
all up as fake anchors, polluting the trunk.

But the synthetic-sessionId rewrite, on its own, does **not** make
the rendered HTML place each subagent's content under its spawning
anchor. After `_reorder_paired_messages` brought every Task/Agent
tool_use ↔ tool_result pair adjacent, all subagent threads were left
clustered at the trunk tail and the level-stack hierarchy collapsed
them all under whichever anchor sat last in render order. The post-merge
refactor (commit `fdd28ec`) added `_relocate_subagent_blocks` — a
single ~50-line post-pass that walks the message list, picks up each
subagent's chunks (identified by the `{trunk}#agent-{agentId}` stamp),
and splices each block right after the trunk Task/Agent tool_result
whose `meta.agent_id` matches. After relocation `_build_message_hierarchy`
nests each subagent's content under its own anchor and
`_cleanup_sidechain_duplicates` fires per-agent.

The teammate identity (name + color) is carried via the inline
`format_task_input_teammate_extras` / `format_task_output_teammate_extras`
on the spawning Task card (§6.1) — not baked into the synthetic id and
no longer surfaced as a session-header pill.

---

## 5. RenderingContext caches (PR #122 + #125 + post-merge refactor)

Three pieces of session-scoped state that downstream formatters read.
All are nested `dict[session_id, ...]` maps for the same architectural
reason: combined transcripts merge multiple sessions, and teammate names
/ task ids aren't globally unique.

### 5.1 `prepare_session_team_names(messages)`

Called once at the top of `generate_template_messages`. Returns
`dict[session_id, team_name]` from the first non-None `teamName` per
session. Passed through `_render_messages` so both regular and branch
session headers can populate `SessionHeaderMessage.team_name`. Branch
headers inherit from the original pre-fork session.

### 5.2 `RenderingContext.teammate_colors`

```python
teammate_colors: dict[str, dict[str, str]]
# session_id → { teammate_id → color }
```

Populated by `_populate_teammate_colors(ctx)` after the message tree is
built. Walks every `TeammateMessage` content; for each block with a
`color` attribute, records `(template_msg.meta.session_id,
block.teammate_id) → color`. First sighting wins per scope.

Without the outer key, alice=blue in session A would silently override
alice=red in session B. The original PR #122 had a flat dict that
CodeRabbit caught.

Both renderers snapshot the nested map at render-start
(`self._teammate_colors_by_session`) and expose
`_colors_for(message) -> dict[teammate_id, color]` for the per-session
lookup.

### 5.3 `RenderingContext.task_subjects` and `task_id_for_tool_use`

```python
task_subjects: dict[str, dict[str, str]]
# session_id → { task_id → subject }

task_id_for_tool_use: dict[str, dict[str, str]]
# session_id → { tool_use_id → task_id }
```

Populated by `_populate_task_metadata(ctx)` (commit `7c364bc`,
post-merge refactor). Walks Task* tool_uses and tool_results:

- **TaskCreate tool_results** are the source of truth: each result
  carries the assigned numeric `task_id` plus the `subject`. Stored as
  `task_subjects[sid][task_id] = subject` and the ↔ link
  `task_id_for_tool_use[sid][matching_tool_use_id] = task_id`.
- **TaskList tool_results** populate the same maps as a snapshot
  fallback for transcripts where the TaskCreate happened in an earlier
  session that isn't in the loaded set.

The `HtmlRenderer` snapshots both maps and uses them in
`title_TaskCreateInput` / `title_TaskUpdateInput` to compose the
compacted title `🛠️ Task #5 <subject> [created]` (§6.1) — the
tool_use card's title carries the human-readable id and subject so the
matching tool_result body becomes redundant and is suppressed entirely
by the empty-pair-suppression pass.

### 5.4 Pipeline order

In `generate_template_messages` (renderer.py):

```
prepare_session_summaries
prepare_session_team_names
_extract_session_hierarchy
_render_messages                    ← creates TemplateMessages, sets team_name
…(filter / pair / reorder passes)…
_reorder_paired_messages
_relocate_subagent_blocks           ← splices subagent chunks under trunk anchors (§4.3)
_build_message_hierarchy            ← needs the final order
_build_message_tree                 ← root_messages + populated children
_cleanup_sidechain_duplicates       ← walks the tree, drops first-User / last-Sub-assistant duplicates
_populate_teammate_colors           ← scans TeammateMessage blocks
_populate_task_metadata             ← scans TaskCreate / TaskList results
```

Each pass is order-sensitive:

- `_relocate_subagent_blocks` MUST run after `_reorder_paired_messages`
  (which is what creates the "all subagents at the tail" layout that
  needs fixing) and before `_build_message_hierarchy` (which freezes
  the parent-child relationships from the final order).
- `_cleanup_sidechain_duplicates` operates on the **tree** built by
  `_build_message_tree`, so the relocation must already have placed
  each subagent's first User and last Sub-assistant under the right
  Task/Agent tool_result for the per-anchor dedup to fire.
- `_populate_task_metadata` reads `TaskCreate` results, so the message
  tree must be built first.

The `_populate_agent_teammates` and `_annotate_subagent_session_headers`
passes from PR #125's first wave were dropped in commit `27e43fb`
(the subagent session header no longer carries a teammate badge — the
teammate identity is on the spawning Task card instead).

---

## 6. Rendering layer (PR #122 + PR #125)

### 6.1 HTML

#### Tool cards — `html/teammate_formatter.py`

A pure data-in / HTML-out module (no `self`, no renderer dep). Each
formatter takes a typed model and returns an HTML fragment. The
HtmlRenderer dispatches `format_TeamCreateInput`, `format_TeamCreateOutput`,
etc., to these helpers and threads `self._colors_for(_)` for colorization.

Card shape: `<dl class="teammate-tool-card <variant>">` with key/value
rows. Distinct variants per tool family (`team-card`, `task-create-card`,
`task-update-card`, `send-message-card`, etc.) with a colored left border
keyed off `--cc-color`.

Special cases:

- `format_teamdelete_output` surfaces the active-members list as colored
  badges when cleanup was refused.
- `format_tasklist_output` renders an HTML `<table class="task-list">`
  with per-row `status-<value>` classes (one of `completed`,
  `in_progress`, `pending`, `blocked`, `deleted`, or `unknown`) for
  styling.
- `format_task_input_teammate_extras` and
  `format_task_output_teammate_extras` append a second `<dl>` to the
  base Task rendering when teammate-spawn fields are present. This is
  also where `Agent` spawn cards get their teammate badge + agent
  metadata, since `Agent` is aliased to `TaskInput` (§3.3).
- `format_sendmessage_input` (commit `47bc50e`) compacts the card:
  the To/Type rows move to the title via `title_SendMessageInput`
  (`✉️ SendMessage to <recipient_badge>` — the leading ✉️ replaces
  the default 🛠️ via the template's "title already has an emoji"
  check), and the message body renders directly as collapsible
  Markdown via `render_markdown_collapsible` with a `send-message-body`
  class. Type is surfaced only when it's not the default `"message"`.

#### Task title compaction (commit `7c364bc`)

`HtmlRenderer.title_TaskCreateInput` and `title_TaskUpdateInput`
override the generic tool title via a shared `_task_title` helper.
They look up the assigned task id and subject in the `task_subjects` /
`task_id_for_tool_use` snapshots (§5.3) and compose:

```
🛠️ Task #5 <subject> [created]
🛠️ Task #5 <subject> [updated]
```

Where `[created]` / `[updated]` is a muted `<span class="task-action">`
tag. The TaskUpdate body card surfaces the new status as a small-caps
pill via `_status_pill` — same `.task-status.status-<value>` styling
used in TaskList rows (lifted out of the `.task-list` selector to
render in both places).

`format_taskcreate_input` drops the now-redundant Subject row;
`format_taskupdate_input` drops the Task row and renders status as
a small-caps pill. Both `format_taskcreate_output` and
`format_taskupdate_output` return `""` (the title carries the id —
TaskUpdate's output stays only when there's a from→to status
transition the title can't show).

#### Empty-pair suppression (commit `7c364bc`)

The post-pass `_flatten_preorder.visit` (renderer.py) skips messages
whose title + html body + children are all empty. This kills the bare
TaskCreate/TaskUpdate tool_result card whose useful content has already
been hoisted into the tool_use's title.

Critical detail: when the suppressed message is the second half of a
pair, the visit also clears the partner's `pair_last` (and
`pair_duration`). Otherwise the surviving tool_use renders with the
flat-bottom `pair_first` border + zero margin-bottom that CSS gives a
pair_first card, expecting a companion that never arrives.

#### `<teammate-message>` cards — `format_teammate_content`

One `<div class="teammate-message">` per block, with a colored left
border, `<div class="teammate-message-header">` carrying a
`<span class="teammate-badge">` (icon + teammate_id), an optional
italicized summary, and a Markdown-rendered body. Surrounding non-block
text appears in `<div class="teammate-surrounding-text">` wrappers.

`teammate_id="system"` blocks gain the `teammate-system` class for
neutral palette styling.

#### Session-header team badge (PR #125 #1)

`format_session_header_content` in
[`html/system_formatters.py`](../claude_code_log/html/system_formatters.py)
appends one pill next to the title when `team_name` is set:

```html
<span class="session-team-badge"
      style="--cc-color: var(--cc-purple); ...">
  <span class="session-team-icon">👥</span>Team: <name>
</span>
```

The `▎teammate` pill on subagent session headers, the
`<details class="subagent-session-block">` collapse, and the
`SessionHeaderMessage.{teammate_id, teammate_color, collapsed_by_default}`
fields they consumed all landed earlier in PR #125 and were dropped by
commit `27e43fb`. The `_relocate_subagent_blocks` pass (§4.3) inlines
each subagent's content under its spawning Task/Agent card, and
`_render_messages` no longer creates a standalone session header for
synthetic `#agent-` sessions (commit `fdd28ec`) — so there's no
subagent-session header anywhere to put a teammate pill on. The
identity is on the spawning Task card via
`format_task_input_teammate_extras` instead.

#### CSS — `templates/components/teammate_styles.css`

Named palette tokens used by every teammate-related rendering:

```css
--cc-blue, --cc-cyan, --cc-green, --cc-yellow, --cc-orange,
--cc-red, --cc-pink, --cc-purple, --cc-gray, --cc-system
+ matching --cc-<color>-bg tints
```

Plus `.teammate-message`, `.teammate-tool-card` and family,
`.task-list`, `.task-status.status-*` (lifted out of `.task-list` so it
renders in TaskUpdate cards too), `.task-action` (for the muted
`[created]`/`[updated]` tag), `.session-team-badge`, and supporting
selectors.

Teammate messages use **left-aligned** styling (not right like normal
user messages) — they're not from the human user and the WhatsApp-style
right alignment would be misleading. This was an explicit ask in #91.

#### Timeline integration

`CSS_CLASS_REGISTRY` in
[`html/utils.py`](../claude_code_log/html/utils.py) maps
`TeammateMessage → ["user", "teammate"]` so the outer DOM class carries
both. `html/templates/components/timeline.html` has a dedicated
`classList.includes('teammate')` branch (preceding the generic `.find`
so it wins over the `user` class) plus a `messageTypeGroups` entry and
`groupOrder` slot — teammate messages appear as their own row in the
timeline.

### 6.2 Markdown

Mirrors HTML where possible. Markdown can't carry CSS color, so a
colored-circle emoji convention preserves the at-a-glance signal:

```python
_COLOR_CIRCLE = {
    "blue": "🔵",  "cyan": "🟦",   "green": "🟢",
    "yellow": "🟡", "orange": "🟠", "red": "🔴",
    "pink": "🌸",   "purple": "🟣", "gray": "⚪",
    "system": "⬛", "default": "⚪",
}
_teammate_marker(name, color) → "🔵 `alice`"
```

`_teammate_marker` routes the name through `_inline_code` (PR #125
CodeRabbit fix #4), which adaptively widens the backtick fence when the
value itself contains backticks. CommonMark explicitly does *not* honor
backslash escapes inside code spans, so the older `replace("\`", "\\`")`
defense was wrong; this is the correct recipe.

`format_TeammateMessage` renders one `> blockquote` per block, headed by
`{circle} **{teammate_id}** · *{summary}*`. System blocks use the ⬛
override.

`format_TaskListOutput` produces a Markdown pipe table. A `_table_cell`
helper escapes `|` and replaces `\n` with `<br>` on every cell (PR #122
CodeRabbit fix — boundary hygiene). Owner cells route through
`_teammate_marker` and skip the cell-escape pass since their format is
deterministic.

`title_SessionHeaderMessage` (PR #125 cea8896, simplified by `27e43fb`)
surfaces the team_name when present:

- Plain trunk session: ``📋 Session `abc12345` ``
- With summary: ``📋 Session `abc12345`: <summary>``
- Team-active session: appends `` — Team: `<name>` ``

The earlier ``📋 Subagent 🔵 `alice` `` variant from the first wave
of PR #125 was removed when synthetic-subagent session headers
stopped being created. Backtick handling on the team_name suffix
uses the same `_inline_code` helper.

---

## 7. Index integration (PR #125 #5)

The project-listing index page surfaces team membership per project.

### 7.1 Cache schema (migration 005)

```sql
ALTER TABLE sessions ADD COLUMN team_name TEXT;
```

Additive, backward-compatible. Existing rows get `NULL`.
`SessionCacheData.team_name: Optional[str] = None` accepts either.

Both readers (`get_cached_project_data` and the archived-session loader)
guard `"team_name" in row.keys()` so a fresh load against an unmigrated
DB still works until the migration runs.

### 7.2 Population

Two cache-build paths in [`converter.py`](../claude_code_log/converter.py):

- `_build_session_data_from_messages` (no-cache fallback path) — captures
  the first non-None `teamName` per session as it walks raw messages.
- `_update_cache_with_session_data` (incremental path) — same shape,
  same first-sighting-wins.

### 7.3 Project aggregation

Every `project_summaries` construction site (cached / fresh / archived)
gains a `team_names: list[str]` field — sorted distinct values across
the project's sessions. The fallback (no-cache) path **filters warmup
and agent-only sessions** to match what the cached path already does
(PR #125 CodeRabbit fix #1) — otherwise a warmup session that happened
to carry `teamName` would surface in the project card via fallback but
not via cache.

`TemplateProject.team_names` exposes the sorted list to the index
template.

### 7.4 Template

`templates/index.html` adds a stat row, conditional on
`project.team_names`:

- Single team: `👥 Team: <code>x</code>`
- Multiple: `👥 Teams (N): <code>a</code>, <code>b</code>, …`

Non-teammate projects render unchanged.

---

## 8. Test fixture

[`test/test_data/teammates/`](../test/test_data/teammates/) contains a
synthetic transcript exercising every shape:

```
ef000000-…-001.jsonl                     # main session
ef000000-…-001/subagents/
  agent-aaaa111111111111.jsonl           # alice subagent
  agent-bbbb222222222222.jsonl           # bob subagent
README.md                                # fixture description
```

The fixture is designed to exercise both linking pathways:

- **alice** links via the **primary path**: her tool_result carries
  `toolUseResult.agentId = "aaaa111111111111"`.
- **bob** links via the **fallback path**: his tool_result carries the
  metadata-tail `agentId:` only (no `toolUseResult.agentId`), and his
  agent-jsonl's first entry wraps the prompt in a
  `<teammate-message teammate_id="team-lead">…</teammate-message>` block
  matching the spawning Task's `prompt` input.

Tests:

- [`test/test_teammates_parsing.py`](../test/test_teammates_parsing.py) —
  unit tests for every parser, plus end-to-end fixture loading,
  session-scoping regression (`test_teammate_colors_are_session_scoped`),
  and prompt-hash ambiguity guard
  (`test_identical_prompts_do_not_collide`). The post-merge refactor
  (commit `fdd28ec`) removed four obsolete tests that targeted the
  dropped `agent_teammates` / `teammate_id` fields:
  `test_annotate_subagent_handles_nested_agents`,
  `test_agent_teammates_are_session_scoped`,
  `test_subagent_session_headers_carry_teammate_badge`,
  `test_agent_teammates_populated_from_task_pairs`.
  `test_agent_session_gets_its_own_header` was renamed to
  `test_agent_session_has_no_separate_header` to assert the new
  inline-only behaviour.
- [`test/test_dag_integration.py`](../test/test_dag_integration.py) —
  also updated by `fdd28ec` to reflect that subagent content now
  appears inline under the spawning anchor rather than as a separate
  session header.
- [`test/test_teammates_browser.py`](../test/test_teammates_browser.py) —
  Playwright assertions on computed colors, table shape, and badge
  consistency. (The `<details>` collapse coverage is no longer
  applicable.)
- [`test/test_snapshot_html.py`](../test/test_snapshot_html.py) and
  [`test/test_snapshot_markdown.py`](../test/test_snapshot_markdown.py) —
  pin the rendered shape via syrupy.

Note: the snapshot tests use `load_transcript(main_jsonl)` (single-file
loader) which doesn't exercise `_integrate_agent_entries` and therefore
doesn't include the relocated subagent content in the snapshotted
output. The browser test uses `load_directory_transcripts` (the path
that does) to cover that. A future tightening could move the snapshot
tests to the directory loader for fuller coverage.

---

## 9. Coverage against #91

Issue #91's "we have to support" list, mapped to the as-built state:

| #91 requirement | Status | Where |
|---|---|---|
| TeamCreate (call + result) | ✓ | typed model, HTML/MD card, snapshot |
| TaskCreate (call + result) | ✓ | typed model, HTML/MD card |
| Task with teammate-spawn fields | ✓ | `TaskInput.{name, team_name, mode, run_in_background}` + extras card |
| TaskUpdate (with colored owner) | ✓ | typed model, owner badge uses teammate color |
| TaskList (call + result) | ✓ | typed model, HTML table, MD pipe-table |
| SendMessage (call + result) | ✓ | typed model, colored target/recipient badge |
| TeamDelete (success + active-members) | ✓ | typed model, parses cleanup-failure message |
| User as container for `<teammate-message>` | ✓ | `TeammateMessage` with `blocks: list[TeammateMessageBlock]` |
| Multiple blocks intermingled in one entry | ✓ | per-block iteration, per-block card |
| `teammate_id="system"` notifications | ✓ | `is_system` flag, distinct neutral palette |
| `teamName` per entry | ✓ | propagated via `MessageMeta.team_name`, surfaced as session-header badge |
| Color associated with teammate name | ✓ | `RenderingContext.teammate_colors` (session-scoped) |
| Color used in TaskUpdate / SendMessage / TaskList owner / Active-members | ✓ | every dispatcher passes `_colors_for(message)` |
| **Whole transaction visible** (subagent JSONL "under the hood" activity) | ✓ | subagent files loaded, integrated via primary or prompt-hash linking, content threaded inline under the spawning Task/Agent tool_result via `_relocate_subagent_blocks` (commit `fdd28ec`); the earlier `<details>` collapse was dropped in `27e43fb` |
| Subagent linking via `agentId` (primary) | ✓ | `parse_agent_result_metadata` + `_integrate_agent_entries` |
| Subagent linking via prompt-hash (fallback) | ✓ | `_link_subagents_by_prompt_hash` (with collision-safe pop) |
| Left-aligned teammate messages | ✓ | `.teammate-message` CSS — no right-align like human user |
| Claude Code icon with color bg, somewhere in header | ✓ | colored teammate badge on the spawning Task/Agent card via `format_task_input_teammate_extras` (HTML); colored emoji circle on Task title (Markdown). The earlier `▎teammate` pill on subagent session headers was dropped along with those headers in commit `27e43fb`. |
| Project-index "Team: …" annotation | ✓ | added in PR #125; SQL migration 005 |

Everything #91 explicitly asks for is implemented.

---

## 10. What's still on the arc

Captured during the user's testing-and-feedback pass on the merged
trilogy. Not part of #91 but adjacent / discovered work:

### 10.1 Standard sub-agents (#79) and async task agents (#90)

Async task agents (#90) are now supported — see
[`agents.md` § 2](agents.md#2-async-task-agents-90) for the as-built
flow (typed models, parsers, the spawn-fold pipeline, detail-level
matrix). The notification's `<result>` body folds onto
`TaskOutput.async_final_answer` of the spawning Task tool_result so
the answer renders at the spawn site, with detail-level-aware drop
of the standalone notification card at LOW.

Standard sync sub-agents (#79) share the same `agentId:` /
`<usage>` metadata-tail shape that `parse_agent_result_metadata`
already handles, so the primary linking path is generic. The
`<teammate-message>`-shaped first-entry that powers the prompt-hash
fallback is teammates-specific; sync sub-agents have plain
user-text first entries and would need a different normalization
to use the fallback path (the current
`find_team_lead_body(text) or text` expression already covers
bare-text bodies, but hasn't been validated against real #79
transcripts).

### 10.2 Detail-level interaction

The detail-level filter (`--detail high|low|minimal|user-only`) drops
tool_use/tool_result content at LOW and below by default, but the
`_LOW_KEEP_TOOLS` whitelist in `renderer.py` exempts the spawn pair so
teammate work survives a LOW rendering:

```python
_LOW_KEEP_TOOLS = {"WebSearch", "WebFetch", "Task", "Agent"}
```

`Agent` is the teammates-feature spawn alias for `Task` (registered in
the tool factory as `"Agent": TaskInput`); both names need to be
whitelisted because real Claude Code teammate transcripts emit `Agent`
rather than `Task`. With this in place:

- The Agent / Task tool_use card stays visible at LOW.
- The matching tool_result card stays too, so the agent's response and
  `agent_metadata` (`agent_id`, `worktree_path`, usage, etc.) survive.
- Subagent sidechain content (the "rest of the conversation" inside
  the agent thread) is still filtered out at LOW by the broader
  `is_sidechain` rule — that's the intended trade-off; the
  spawn-and-result pair is enough to scan.
- MINIMAL still strips everything except user/assistant text; the
  `Agent` whitelist applies to LOW only.

Regression coverage for both halves of the contract:
`TestExperimentsWorktreesTeammates::test_low_detail_preserves_agent_spawns`
and `…::test_minimal_detail_strips_agent_spawns` in
[`test/test_integration_realistic.py`](../test/test_integration_realistic.py).

### 10.3 Snapshot test coverage

Both `test_teammates_fixture_html` and `test_teammates_fixture_markdown`
use `load_transcript(main_jsonl)` — they pin the main session's
rendering but don't exercise subagent session headers (those only
appear via `load_directory_transcripts`). The browser test covers that
path explicitly.

A snapshot tightening could move both fixture snapshots to the
directory loader for fuller pinning.

### 10.4 Manual testing feedback (post-merge refactor)

Real-world testing on `ef958aa1-…` (an experiments/worktrees teammates
session) surfaced the issues the post-merge refactor (`fd993f2` →
`7c364bc`) addresses. The current state captured in this document is
the result of that pass:

- `Agent` tool now renders as a Task card (not generic param table).
- Sidechain dedup now fires for both `Task` and `Agent` spawns.
- All wave-1 + wave-2 subagents correctly nest under their respective
  spawning anchors via `_relocate_subagent_blocks`.
- Empty subagent session headers no longer pollute the document tail.
- TaskCreate / TaskUpdate are compacted (title carries the id +
  subject; bare result cards are suppressed).

Remaining fine-tuning ideas (not blocking):

- The `isolation` field on real `Agent` inputs survives parsing via
  Pydantic's `extra="ignore"` but isn't yet surfaced. A possible
  enhancement is to fold it into the teammate badge as
  `Teammate ▎alice (isolation: worktree)`.
- A few snapshot fixtures could move to the directory loader for fuller
  coverage of the relocation pass (see §8).

---

## 11. Cross-references

### Code

- [`claude_code_log/factories/agent_metadata_factory.py`](../claude_code_log/factories/agent_metadata_factory.py)
- [`claude_code_log/factories/teammate_factory.py`](../claude_code_log/factories/teammate_factory.py)
- [`claude_code_log/factories/tool_factory.py`](../claude_code_log/factories/tool_factory.py)
  (TOOL_INPUT_MODELS / TOOL_OUTPUT_PARSERS registries)
- [`claude_code_log/html/teammate_formatter.py`](../claude_code_log/html/teammate_formatter.py)
- [`claude_code_log/html/templates/components/teammate_styles.css`](../claude_code_log/html/templates/components/teammate_styles.css)
- `_link_subagents_by_prompt_hash`,
  `_cleanup_sidechain_duplicates` (`{Task, Agent}` set) in
  [`claude_code_log/converter.py`](../claude_code_log/converter.py)
- `_relocate_subagent_blocks`, `_populate_teammate_colors`,
  `_populate_task_metadata` in
  [`claude_code_log/renderer.py`](../claude_code_log/renderer.py)
- `title_TaskCreateInput` / `title_TaskUpdateInput` /
  `_task_title` / `_status_pill` in
  [`claude_code_log/html/renderer.py`](../claude_code_log/html/renderer.py)
- `_flatten_preorder.visit` empty-pair suppression in
  [`claude_code_log/renderer.py`](../claude_code_log/renderer.py)
- [`claude_code_log/migrations/005_session_team_name.sql`](../claude_code_log/migrations/005_session_team_name.sql)

### Other dev-docs

- [`dev-docs/agents.md`](agents.md) — agent-spawning overview
  (sync sub-agents, async task agents, teammates). The
  async-agents § documents the `<task-notification>` flow and the
  Phase 3 fold pipeline; teammates is the special-case the present
  doc covers in detail.
- [`dev-docs/dag.md`](dag.md) — session DAG, sub-agent integration
  via `_integrate_agent_entries`. Read this first if the synthetic
  sessionId / parent-rewrite mechanics are unclear.
- [`dev-docs/messages.md`](messages.md) — full message-type taxonomy.
  `TeammateMessage` slots into the User content variants.
- [`dev-docs/rendering-architecture.md`](rendering-architecture.md) —
  the layered pipeline that `_populate_teammate_colors` etc. extend.

### Issues and PRs

- Parent: [#91 Support teammates](https://github.com/daaain/claude-code-log/issues/91).
- Adjacent: [#79](https://github.com/daaain/claude-code-log/issues/79)
  (sync sub-agent linking),
  [#90](https://github.com/daaain/claude-code-log/issues/90)
  (async task agents),
  [#94](https://github.com/daaain/claude-code-log/issues/94) (session
  state propagation, related work in `work/session-state-propagation.md`).
- Prerequisite: [#115](https://github.com/daaain/claude-code-log/pull/115)
  (parallel-Task anchor preservation; merged before PR 1).
- Trilogy: [#117](https://github.com/daaain/claude-code-log/pull/117)
  (parsing + data model),
  [#122](https://github.com/daaain/claude-code-log/pull/122)
  (rendering),
  [#125](https://github.com/daaain/claude-code-log/pull/125)
  (stitching + headers + index).

### Plan documents

The original `work/teammates-plan.md` was removed when this as-built
reference landed. Decisions and trade-offs from the planning phase now
live in this document and in the trilogy's commit messages / PR
descriptions.

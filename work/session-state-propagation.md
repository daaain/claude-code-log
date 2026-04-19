# Session State Propagation (issue #94 follow-up)

## Status: Proposed — not started

## Context

Claude Code writes per-session state snapshots into the transcript as
standalone lines:

```json
{"type":"custom-title","customTitle":"CCL (Monk)","sessionId":"3a97..."}
{"type":"agent-name","agentName":"CCL (Monk)","sessionId":"3a97..."}
{"type":"agent-color","agentColor":"purple","sessionId":"3a97..."}
{"type":"permission-mode","permissionMode":"acceptEdits","sessionId":"3a97..."}
```

PR #113's silent-skip commit makes the loader drop these without noise.
This plan covers the follow-up: actually *use* the information so the
rendered transcript surfaces the agent identity that was in effect
when each message was written.

## Data shape

All four types share the same shape:

- `type` + `sessionId` + single payload field (`agentName`, `agentColor`,
  `customTitle`, `permissionMode`).
- **No `uuid`, no `parentUuid`, no `timestamp`.** They are not DAG nodes.

Real-world observations on the monk session
(`3a974998-3a21-4f1d-bc20-39874cf2f2f3.jsonl`, 900+ lines):

- Line 1 is the first state snapshot — session files open with a
  metadata header (`custom-title` first, then `agent-name`, then
  `agent-color` a few lines later).
- `permission-mode` recurs heavily — one snapshot per toggle, most
  with identical `acceptEdits` payload (redundant but harmless).
- `/rename` triggers a clustered update at the matching line:
  `custom-title` → `agent-name` → `permission-mode`, repeated across
  the conversation wherever the user renamed.

Each session file is self-contained: a resume session starts with its
own fresh state header, so there is **no cross-file inheritance** to
worry about.

## Target rendering

Two decorations on conversational message titles:

1. Assistant titles become `Assistant · CCL (Monk)` (or similar
   separator) when `agentName` has a known value.
2. When `agentColor` is set, the name is wrapped in
   `<span class="agent-color-purple">…</span>` so CSS can tint it.

`permissionMode` is **not** proposed for UI surfacing in this first
pass — noisy and low signal. Keep it parsed-but-dropped unless a
future concrete need appears.

## Propagation model

State messages have no temporal fields, so propagation must be
**file-position-based, not DAG-based**:

- Keep a `current_state` dict while iterating lines in
  `load_transcript`.
- On a state-type line, update the relevant field in `current_state`.
- On a conversational line (anything with `uuid`+`sessionId`), record
  the current `current_state` against that entry.

This is correct even for DAG forks: the DAG is a logical view over
write order. A message's state *when it was written* is unambiguous —
the most recent state change above it in the same file.

Cross-session case (parent session S1 resumes as S2 in a new file):
each file re-establishes its own state on line 1, so no
cross-file bridge is needed.

## Implementation sketch

### 1. `claude_code_log/models.py` — MessageMeta

Add four optional fields (all default `None`):

```python
agent_name: Optional[str] = None
agent_color: Optional[str] = None
custom_title: Optional[str] = None
permission_mode: Optional[str] = None
```

### 2. `claude_code_log/converter.py` — load_transcript

Replace the `elif entry_type in SILENT_SKIP_TYPES: pass` branch with a
narrower silent-skip (keep `file-history-snapshot`, `last-prompt`) plus
a dedicated state-update branch:

```python
SESSION_STATE_TYPES = {
    "agent-name": "agent_name",
    "agent-color": "agent_color",
    "custom-title": "custom_title",
    "permission-mode": "permission_mode",
}
SESSION_STATE_PAYLOAD = {
    "agent-name": "agentName",
    "agent-color": "agentColor",
    "custom-title": "customTitle",
    "permission-mode": "permissionMode",
}

# At load_transcript top:
current_state: dict[str, str | None] = {f: None for f in SESSION_STATE_TYPES.values()}
entry_state: dict[str, dict[str, str | None]] = {}  # uuid -> snapshot

# In the dispatch:
elif entry_type in SESSION_STATE_TYPES:
    field = SESSION_STATE_TYPES[entry_type]
    value = entry_dict.get(SESSION_STATE_PAYLOAD[entry_type])
    if isinstance(value, str):
        current_state[field] = value
    # silently skipped from messages either way
```

For every conversational entry pushed to `messages`, stash a copy of
`current_state` in `entry_state[entry.uuid]`.

Return `entry_state` as a second tuple element, or attach per-entry
via private attribute (`entry._session_state = dict(current_state)`).
The private-attr approach keeps the public signature stable at the
cost of an out-of-band channel; the tuple approach is more explicit
but ripples through every `load_transcript` call site.

Recommendation: **private attribute**, since the state is logically
part of the entry context (like `agentId`) and callers who don't care
stay unchanged. `BaseTranscriptEntry` already tolerates it (pydantic
v2 allows non-field attributes on instances).

### 3. `claude_code_log/factories/meta_factory.py`

Forward the private attrs into `MessageMeta` via `getattr(..., None)`:

```python
return MessageMeta(
    ...,
    agent_name=getattr(transcript, "_session_agent_name", None),
    agent_color=getattr(transcript, "_session_agent_color", None),
    ...,
)
```

### 4. Renderers — title adjustment

`claude_code_log/renderer.py::title_AssistantTextMessage` (HTML base)
and `claude_code_log/markdown/renderer.py::title_AssistantTextMessage`:

```python
base = "Sub-assistant" if message.meta.is_sidechain else "Assistant"
name = message.meta.agent_name
color = message.meta.agent_color
if name:
    if color:
        return f'{base} · <span class="agent-color-{color}">{name}</span>'
    return f"{base} · {name}"
return base
```

HTML escape `name` (agent names can contain arbitrary characters).
Markdown renderer would emit plain text (no color span).

### 5. CSS

`claude_code_log/html/templates/components/message_styles.css` — map
Claude Code's color vocabulary (observed: `purple`, `orange`, plus
the standard palette) to CSS custom properties already in the theme:

```css
.agent-color-purple { color: var(--cc-purple, #a855f7); }
.agent-color-orange { color: var(--cc-orange, #f97316); }
/* ... */
```

### 6. Tests

- `test_silent_skip.py`: add a test that checks `current_state`
  bookkeeping — feed a sequence of state+conversational entries,
  assert the conversational ones carry the expected snapshot on
  their `MessageMeta`.
- Snapshot tests will regenerate — any transcript that contains these
  state types will now show the decorated title. Manual review of
  the diff will be needed to confirm intent.

### 7. Cache concerns

`cache.py` persists rendered HTML and pre-parsed session metadata.
Two risk points:

- Cached parsed sessions may bypass `load_transcript` on cache hits.
  Verify that the state snapshot travels through the cache (or is
  stored alongside) — otherwise cache-hit renders will show bare
  titles.
- Schema version bump likely required; old caches won't have the
  state data.

Audit `cache.py` for what it stores per session and extend
accordingly.

## Open questions

1. **Separator**: `·`, `:`, `—`, `/`? User sketched `"Assistant . CCL
   (monk)"`. Prefer a visually clean glyph; `·` (U+00B7) reads well
   and doesn't collide with code syntax.
2. **Color palette**: which colors does Claude Code actually emit?
   Scan real transcripts for the set of `agentColor` values before
   writing CSS.
3. **Permission mode rendering**: surface or not? A subtle badge on
   messages whose mode ≠ `default` could be useful for auditing. Punt
   unless someone asks.
4. **Nav / session card**: the session header is an obvious second
   target — `agent-name` could replace or augment the session title.
   Scope it out as a follow-up unless the title-level change
   demonstrates clear value first.

## Risks

- Snapshot test churn is unavoidable and will be noisy. Coordinate
  with whoever last regenerated snapshots.
- The private-attr channel on pydantic entries is clever-but-subtle;
  add a short docstring so future readers don't wonder why
  `entry._session_agent_name` exists.
- Markdown renderer has its own `title_AssistantTextMessage` and also
  a `_last_heading_category` compact-mode tracker. Agent-name changes
  should not bust heading categorisation (the tracked key is derived
  from the pre-colon prefix, so this probably already works — verify).

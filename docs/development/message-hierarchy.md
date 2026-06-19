# Message Hierarchy and Fold State

> See [application_model.md](application_model.md) for the system overview.

## Message Hierarchy

The virtual parent/child structure of a conversation determines how folding works:

```
Session (level 0)
└── User message (level 1)
      ├── System: command/error (level 2)
      └── Assistant response (level 2)
            ├── System: info/warning (level 3)
            ├── Tool: Read ─────────────┐ (level 3)
            │   └── Tool result ────────┘ paired, fold together
            └── Tool: Task ─────────────┐ (level 3)
                  └── Task result ──────┘ paired, fold together
                      └── Sub-assistant response (level 4, sidechain)
                            ├── Sub-tool: Edit ──────┐ (level 5)
                            │   └── Sub-tool result ─┘ paired
                            └── ...
```

**Notes:**
- **Paired messages** (tool_use + tool_result, thinking + assistant) fold together as a single visual unit
- **Sidechain (sub-agent) messages** appear nested under the Task tool that spawned them
- **Deduplication**: When a sub-agent's final message duplicates the Task result, it's replaced with a link to avoid redundancy

At each level, we want to fold/unfold immediate children or all children.

## Fold Bar Behavior

The fold bar has two buttons with three possible states:

### State Definitions

| State | Button 1 | Button 2 | Visibility | Description |
|-------|----------|----------|------------|-------------|
| **A** | ▶ | ▶▶ | Nothing visible | Fully folded |
| **B** | ▼ | ▶▶ | First level visible | One level unfolded |
| **C** | ▼ | ▼▼ | All levels visible | Fully unfolded |

**Note**: The state "▶ ▼▼" (first level folded, all levels unfolded) is **impossible** and should never occur.

## State Transitions

```
            ┌────────────────────────────────┐
  ┌────────►│       State A (▶ / ▶▶)        │◄────────┐
  │         │       Nothing visible          │         │
  │         └────────────────────────────────┘         │
  │                │                   │               │
  │      Click ▶   │                   │  Click ▶▶    │
  │     (unfold 1) │                   │  (unfold all) │
  │                ▼                   ▼               │
  │      ┌─────────────┐      ┌─────────────┐         │
  │      │  State B    │      │  State C    │         │
  │      │  (▼ / ▶▶)  │      │  (▼ / ▼▼)  │         │
  │      │  First      │      │  All        │         │
  │      │  level      │      │  levels     │         │
  │      │  visible    │      │  visible    │         │
  │      └─────────────┘      └─────────────┘         │
  │         │       │              │       │          │
  │  Click ▼│       └── ▶▶ ↔ ▼▼ ──┘       │Click ▼   │
  │         │       (unfold all / fold 1)  │          │
  └─────────┘                              └──────────┘
       (fold all)                            (fold all)
```

## Simplified Transition Table

| Current State | Click Button 1 | Result | Click Button 2 | Result |
|---------------|----------------|--------|----------------|--------|
| **A: ▶ ▶▶** (nothing) | ▶ (unfold 1) | **B: ▼ ▶▶** (first level) | ▶▶ (unfold all) | **C: ▼ ▼▼** (all levels) |
| **B: ▼ ▶▶** (first level) | ▼ (fold 1) | **A: ▶ ▶▶** (nothing) | ▶▶ (unfold all) | **C: ▼ ▼▼** (all levels) |
| **C: ▼ ▼▼** (all levels) | ▼ (fold 1) | **A: ▶ ▶▶** (nothing) | ▼▼ (fold all) | **B: ▼ ▶▶** (first level) |

## Key Insights

1. **Button 1 (fold/unfold one level)**:
   - From State A (▶): Unfolds to first level → State B (▼)
   - From State B or C (▼): Folds completely → State A (▶)
   - **Always toggles between "nothing" and "first level"**

2. **Button 2 (fold/unfold all levels)**:
   - From State A (▶▶): Unfolds to all levels → State C (▼▼)
   - From State B (▶▶): Unfolds to all levels → State C (▼▼)
   - From State C (▼▼): Folds to first level (NOT nothing) → State B (▼ ▶▶)
   - **When unfolding (▶▶), always shows ALL levels. When folding (▼▼), goes back to first level only.**

3. **Coordination**:
   - When button 1 changes, button 2 updates accordingly
   - When button 2 changes, button 1 updates accordingly
   - The impossible state "▶ ▼▼" is prevented by design

## Initial State

- **Sessions and User messages**: Start in **State B** (▼ ▶▶) - first level visible
- **Assistant, System, Thinking, Tools**: Start in **State A** (▶ ▶▶) - fully folded

## Example Flow

**Starting from State A (fully folded):**

1. User sees: `▶ 2 messages    ▶▶ 125 total`
2. Clicks ▶▶ (unfold all) → Goes to State C, sees everything
3. Now sees: `▼ fold 2    ▼▼ fold all below`
4. Clicks ▼▼ (fold all) → Goes back to State B, sees only first level
5. Now sees: `▼ fold 2    ▶▶ fold all 125 below`
6. Clicks ▼ (fold one) → Goes to State A, sees nothing
7. Back to: `▶ 2 messages    ▶▶ 125 total`
8. Clicks ▶ (unfold one) → Goes to State B, sees first level
9. Now sees: `▼ fold 2    ▶▶ fold all 125 below`

This creates a natural exploration pattern: nothing → all levels → first level → nothing → first level.

## Dynamic Tooltips

Fold buttons display context-aware tooltips showing what will happen on click (not current state):

| Button State | Tooltip |
|--------------|---------|
| ▶ (fold-one, folded) | "Unfold (1st level)..." |
| ▼ (fold-one, unfolded) | "Fold (all levels)..." |
| ▶▶ (fold-all, folded) | "Unfold (all levels)..." |
| ▼▼ (fold-all, unfolded) | "Fold (to 1st level)..." |

## Implementation Notes

- **Performance**: Descendant counting is O(n) using cached hierarchy lookups
- **Paired messages**: Pairs are counted as single units in child/descendant counts
- **Labels**: Fold bars show type-aware labels like "3 assistant, 4 tools" or "2 tool pairs"

---

## Hierarchy System Architecture

The hierarchy system in `renderer.py` determines message nesting for the fold/unfold UI.
It consists of three main functions:

### `_get_message_hierarchy_level(css_class, is_sidechain) -> int`

Determines the hierarchy level for a message based on its CSS class and sidechain status.

**Level Definitions:**

| Level | Message Types | Description |
|-------|---------------|-------------|
| 0 | `session-header` | Session dividers |
| 1 | `user`, `teammate` | User messages (top-level conversation), including TeammateMessage entries |
| 2 | `assistant`, `thinking`, `system` (commands/errors) | Direct responses to user |
| 3 | `tool_use`, `tool_result`, `system-info`, `system-warning`, `task_notification` | Nested under assistant (the spawning Task for async-agent notifications, the calling assistant for everything else) |
| 4 | `user`/`teammate`/`assistant`/`thinking` (sidechain) | Sub-agent responses (from Task tool); also the team-lead's wrapped prompt to a teammate |
| 5 | `tool_use sidechain`, `tool_result sidechain` | Sub-agent tools |

**Decision Logic:**

```
css_class contains?    is_sidechain?    Result
────────────────────   ──────────────   ──────
"user" or "teammate"   false            Level 1
"user" or "teammate"   true             Level 4
"system-info/warning"  false            Level 3
"system"               false            Level 2
"assistant/thinking"   true             Level 4
"tool"                 true             Level 5
"assistant/thinking"   false            Level 2
"tool"                 false            Level 3
(default)              -                Level 1
```

**Edge Cases:**
- Plain sidechain user messages that duplicate the Task input prompt (`UserTextMessage` content matching the spawning Task's prompt) get pruned by `_cleanup_sidechain_duplicates` *after* the tree is built — they still go through the level dispatch first.
- `TeammateMessage`-shaped sidechain users (the team-lead's wrapped prompt) are kept visible and slot in at Level 4 alongside other sidechain user/assistant content; the dedup pass intentionally doesn't touch them.
- `system-info` and `system-warning` are at level 3 (tool-related notifications).
- `system` (commands/errors) without info/warning are at level 2.

### `_build_message_hierarchy(messages) -> None`

Builds `message_id` and `ancestry` for all messages using a stack-based approach.

**Algorithm:**

1. Maintain a stack of `(level, message_id)` tuples
2. For each message:
   - Determine level via `_get_message_hierarchy_level()`
   - Pop stack until finding appropriate parent (level < current)
   - Build ancestry from remaining stack entries
   - Push current message onto stack
3. Session headers use `session-{uuid}` format for navigation
4. Other messages use `d-{counter}` format

**Ancestry Example:**

```
Session (session-abc)           ancestry: []
└── User (d-0)                  ancestry: ["session-abc"]
    └── Assistant (d-1)         ancestry: ["session-abc", "d-0"]
        └── Tool use (d-2)      ancestry: ["session-abc", "d-0", "d-1"]
            └── Tool result (d-3) ancestry: ["session-abc", "d-0", "d-1", "d-2"]
```

**Important:** This function must be called after all reordering operations (pair reordering,
sidechain reordering) to ensure hierarchy reflects final display order.

### `_mark_messages_with_children(messages) -> None`

Calculates descendant counts for fold bar labels.

**Computed Fields:**

| Field | Description |
|-------|-------------|
| `has_children` | True if message has any children |
| `immediate_children_count` | Count of direct children only |
| `total_descendants_count` | Count of all descendants recursively |
| `immediate_children_by_type` | Dict mapping css_class to count |
| `total_descendants_by_type` | Dict mapping css_class to count |

**Algorithm:**

1. Build O(1) lookup index of messages by ID
2. For each message with ancestry:
   - Skip `pair_last` messages (pairs count as one unit)
   - Increment immediate parent's `immediate_children_count`
   - Increment all ancestors' `total_descendants_count`
   - Track counts by message type for detailed labels

**Time Complexity:** O(n) where n is message count

### JavaScript Fold Controls Interaction

The JavaScript in `templates/components/fold_bar.html` uses these computed values:

1. **Ancestry classes**: Each message has `d-{n}` classes from ancestry for CSS targeting
2. **Child counts**: Displayed in fold bar buttons ("▶ 3 messages")
3. **Descendant counts**: Displayed in fold-all button ("▶▶ 125 total")
4. **Type counts**: Used for descriptive labels ("2 assistant, 4 tools")

**Visibility Control:**

```javascript
// Toggle immediate children visibility
document.querySelectorAll(`.d-${messageId}`).forEach(child => {
    child.classList.toggle('filtered-hidden');
});

// Toggle all descendants visibility
ancestry.forEach(ancestorId => {
    document.querySelectorAll(`.d-${ancestorId}`).forEach(child => {
        child.classList.toggle('filtered-hidden');
    });
});
```

### Sidechain (Sub-agent) Handling

Messages from Task tool sub-agents are handled specially:

1. **Identification**: `isSidechain: true` in JSONL → `sidechain` in css_class
2. **Level assignment**: Sidechain `user`/`teammate`/`assistant`/`thinking` at level 4, sidechain tools at level 5
3. **Reordering**: Sidechain messages appear under their Task/Agent tool_result via `_relocate_subagent_blocks`
4. **First-prompt dedup**: After tree build, `_cleanup_sidechain_duplicates` prunes the first sidechain `UserTextMessage` when it duplicates the spawning Task's prompt. `TeammateMessage`-shaped sidechain prompts (the team-lead's wrapped prompt) are intentionally kept visible — they go through the level dispatch normally.
5. **Last-response dedup**: Identical trailing sidechain assistant results are replaced with links to the Task tool_result that already shows the same text.

### Paired Message Handling

Paired messages (tool_use + tool_result, thinking + assistant) are handled as units:

1. **Pairing**: `_identify_message_pairs()` links messages via `tool_use_id`
2. **Counting**: Only `pair_first` messages count toward parent's children
3. **Folding**: Both messages fold/unfold together
4. **Display**: Pair duration shown on `pair_last` message

---

## References

- [renderer.py](../claude_code_log/renderer.py) - Message hierarchy functions (lines 1285-1493)
- [transcript.html](../claude_code_log/html/templates/transcript.html) - Fold/unfold JavaScript controls
- [message_styles.css](../claude_code_log/html/templates/components/message_styles.css) - Fold state CSS styles

# CSS Classes for Message Types

> See [application_model.md](application_model.md) for the system overview.

This document provides a comprehensive reference for CSS class combinations used in Claude Code Log HTML output, their CSS rule support status, and pairing behavior.

**Generated from analysis of:** 29 session HTML files (3,244 message elements)
**Last updated:** 2025-12-07

---

## Quick Reference

### Support Status Legend

| Status | Meaning |
|--------|---------|
| ✅ Full | Has dedicated CSS selectors for this combination |
| ⚠️ Partial | Inherits from parent selectors only |
| ❌ None | No CSS rules found |

---

## Base Message Types

| Type | Description | CSS Support |
|------|-------------|-------------|
| `assistant` | Assistant response | ✅ Full |
| `bash-input` | Bash command input | ✅ Full |
| `bash-output` | Bash command output | ✅ Full |
| `image` | User-attached image | ✅ Full |
| `session-header` | Session header divider | ✅ Full |
| `system` | System message (user-initiated) | ✅ Full |
| `system-away-summary` | Away-summary recap | ✅ Full |
| `system-error` | System error (assistant-generated) | ✅ Full |
| `system-info` | System info message | ✅ Full |
| `system-warning` | System warning (assistant-generated) | ✅ Full |
| `thinking` | Extended thinking content | ✅ Full |
| `tool_result` | Tool result (success) | ✅ Full |
| `tool_use` | Tool use message | ✅ Full |
| `user` | Basic user message | ✅ Full |
| `unknown` | Unknown message type | ❌ None |

---

## Modifier Classes

| Modifier | Applied To | Description |
|----------|------------|-------------|
| `compacted` | `user` | Compacted conversation summary |
| `command-output` | `user` | Slash command output content |
| `error` | `tool_result` | Tool execution error |
| `memory` | `tool_use`, `tool_result` | Auto-memory interaction: Read/Write/Edit on a `~/.claude/projects/<slug>/memory/` path (#192). Drives the 🧠 title, the `memory` filter toggle, and the timeline `memory` lane. |
| `pair_first` | Various | First message in a pair |
| `pair_last` | Various | Last message in a pair |
| `pair_middle` | Various | Middle message (never used so far) |
| `sidechain` | Various | Sub-agent (Task) message. The sidechain block under its spawning tool_result is framed by a single tool-green group line + 2em indent — the line continues the spawning card's border color (same pairing principle as the workflow phase/agent group lines). |
| `slash-command` | `user` | Expanded slash command prompt |
| `steering` | `user` | User steering via queue operation |
| `system-info` | `system` | System info level |
| `system-hook` | `system` | Hook execution summary |
| `system-away-summary` | `system` | Away-summary recap (left-aligned, narrative) |
| `workflow_phase` | `tool_use` | Spliced dynamic-workflow phase card (#174). The `tool_use` base keeps it under the "Tool Use" filter toggle; the modifier drives the depth-driven indent + dark-green card/group border, the 🧩 title, and a dedicated timeline lane. |
| `workflow_agent` | `tool_use` | Spliced dynamic-workflow agent card (#174). Same pattern as `workflow_phase` (grey card/group border, 🤖 title, own timeline lane). Its `.children` container indents the agent's grafted side-channel transcript one level further. |

---

## Pairing Behavior

Message pairing creates visual groupings for related messages. The `pair_first` and `pair_last` classes control styling of paired messages.

### Pairing Rules by Type

| Base Type | Can Be `pair_first` | Can Be `pair_last` |
|-----------|---------------------|-------------------|
| `assistant` | No | Yes |
| `bash-input` | Yes | No |
| `bash-output` | No | Yes |
| `system` | Yes | Yes |
| `thinking` | Yes | No |
| `tool_result` | No | Yes |
| `tool_use` | Yes | No |
| `user` | No | Yes |

### Common Pairing Patterns

| First Message | Last Message | Linked By |
|---------------|--------------|-----------|
| `tool_use` | `tool_result` | `tool_use_id` |
| `bash-input` | `bash-output` | Sequential |
| `thinking` | `assistant` | Sequential |
| `user` (slash-command) | `user` (command-output) | Sequential |
| `system` (system-info) | `system` (system-info) | Paired info |

---

## All Class Combinations by Support Level

### ✅ Full Support (25 combinations)

These combinations have dedicated CSS selectors:

| Combination | Description | Occurrences |
|-------------|-------------|-------------|
| `assistant` | Assistant response | 419 |
| `assistant ` | Assistant (paired with thinking) | 104 |
| `assistant sidechain` | Sub-assistant response | 73 |
| `bash-input` | Bash command input | 5 |
| `bash-output` | Bash command output | 5 |
| `image` | Image content | (rare) |
| `session-header` | Session header divider | 29 |
| `system` | System message (user-initiated) | 20 |
| `system system-hook` | Hook summary message | (rare) |
| `system-error` | System error (assistant-generated) | (rare) |
| `system-info` | System info message | 118 |
| `system-warning` | System warning (assistant-generated) | (rare) |
| `thinking` | Thinking content | 199 |
| `thinking  pair_first` | Thinking (first in pair) | 104 |
| `thinking sidechain` | Sub-assistant thinking | (rare) |
| `tool_result` | Tool result (success) | 863 |
| `tool_result error` | Tool result (error) | 83 |
| `tool_result sidechain` | Sub-assistant tool result | 83 |
| `tool_use` | Tool use message | 946 |
| `tool_use sidechain` | Sub-assistant tool use | 84 |
| `user` | Basic user message | 88 |
| `user command-output` | Slash command output | 19 |
| `user compacted` | Compacted user conversation | (rare) |
| `user slash-command` | Slash command invocation | 20 |
| `user steering` | Out-of-band steering input | (rare) |

### ⚠️ Partial Support (7 combinations)

These combinations inherit from parent selectors but have no dedicated rules:

| Combination | Description | Inherits From |
|-------------|-------------|---------------|
| `assistant  pair_last` | Assistant (last in pair) | `.assistant`, `.` |
| `tool_result error sidechain` | Sub-assistant tool error | `.tool_result`, `.error`, `.sidechain` |
| `unknown sidechain` | Unknown sidechain type | `.sidechain` |
| `user compacted sidechain` | Compacted sidechain user | `.user`, `.compacted`, `.sidechain` |
| `user sidechain` | Sub-assistant user prompt (deprecated) | `.user`, `.sidechain` |
| `user slash-command sidechain` | Sidechain slash command | `.user`, `.slash-command`, `.sidechain` |
| `user command-output pair_last` | Command output in pair | `.user`, `.command-output` |

### ❌ No Support (1 combination)

| Combination | Description | Note |
|-------------|-------------|------|
| `unknown` | Unknown message type | Fallback type - should rarely appear |

---

## Fold-Bar Support

The fold-bar component uses `data-border-color` attribute to style borders based on message types. Below shows which combinations have dedicated fold-bar styling.

### Has Fold-Bar Styling (27 combinations)

- `assistant`
- `assistant sidechain`
- `bash-input`
- `bash-output`
- `image`
- `image sidechain`
- `session-header`
- `system`
- `system-away-summary`
- `system-error`
- `system-info`
- `system-warning`
- `thinking`
- `thinking sidechain`
- `tool_result`
- `tool_result error`
- `tool_result error sidechain`
- `tool_result sidechain`
- `tool_use`
- `tool_use sidechain`
- `unknown`
- `unknown sidechain`
- `user`
- `user command-output`
- `user compacted`
- `user compacted sidechain`
- `user sidechain`
- `user slash-command`
- `user slash-command sidechain`

### Missing Fold-Bar Styling (5 combinations)

These combinations appear in HTML but lack dedicated fold-bar border colors:

- `assistant ` (uses base `assistant` color)
- `assistant  pair_last` (uses base `assistant` color)
- `system system-hook` (uses base `system` color)
- `thinking  pair_first` (uses base `thinking` color)
- `user steering` (uses base `user` color)

---

## Detailed Breakdown by Base Type

### `assistant` (596 occurrences, 3 variations)
- 419× `assistant` (standalone)
- 104× `assistant pair_last `
- 73× `assistant sidechain`

### `bash-input` (5 occurrences, 1 variation)
- 5× `bash-input pair_first `

### `bash-output` (5 occurrences, 1 variation)
- 5× `bash-output pair_last `

### `system` (138 occurrences, 3 variations)
- 59× `system pair_first  system-info`
- 59× `system pair_last  system-info`
- 20× `system pair_first `

### `thinking` (303 occurrences, 2 variations)
- 199× `thinking` (standalone)
- 104× `thinking pair_first `

### `tool_result` (1,030 occurrences, 4 variations)
- 863× `tool_result pair_last `
- 83× `tool_result error pair_last `
- 83× `tool_result pair_last  sidechain`
- 1× `tool_result error pair_last  sidechain`

### `tool_use` (1,030 occurrences, 2 variations)
- 946× `tool_use pair_first `
- 84× `tool_use pair_first  sidechain`

### `user` (128 occurrences, 4 variations)
- 88× `user` (standalone)
- 20× `user pair_first  slash-command`
- 19× `user command-output pair_last `
- 1× `user pair_last  slash-command` (unpaired)

---

## Key Observations

1. **Pairing Consistency**: Tools (`tool_use` + `tool_result`) and bash commands (`bash-input` + `bash-output`) always appear as pairs, with `pair_first` on the input/use side and `pair_last` on the output/result side.

2. **Thinking-Assistant Pattern**: `thinking` messages that are paired are always `pair_first`, paired with an `assistant` message that is `pair_last`.

3. **Sidechains**: The `sidechain` modifier appears on:
   - `assistant` messages (73 occurrences)
   - `tool_use` and `tool_result` pairs (84 and 84 occurrences respectively)

4. **Error Handling**: The `error` modifier only appears on `tool_result` messages (84 total error results).

5. **System Messages**: Have 3 variations:
   - System info pairs (118 total, always paired)
   - Generic system pairs (20, `pair_first`)

6. **Slash Commands**: User messages with `slash-command` and `command-output` pair together:
   - `user slash-command` (20 occurrences, `pair_first`)
   - `user command-output` (19 occurrences, `pair_last`)

7. **Rare Cases**:
   - `tool_result` with both `error` and `sidechain` (1 occurrence)
   - `bash-input`/`bash-output` pairs (5 pairs total)

---

## Structural Classes (Not Semantic)

In addition to the semantic classes above, messages include structural classes:

- **Session IDs**: `session-{uuid}` - identifies which session a message belongs to
- **Ancestry Markers**: `d-{number}` - indicates descendant depth in the message tree

These are excluded from semantic analysis but appear in all HTML output.

---

## CSS Selector Statistics

- **Total CSS selectors in templates**: 495
- **Message-related selectors**: 78
- **Fold-bar combinations**: 28
- **Full support combinations**: 25
- **Partial support combinations**: 7
- **No support combinations**: 1

---

## References

- Source: [css_class_combinations_summary.md](/tmp/css_class_combinations_summary.md)
- Source: [css_rules_analysis.md](/tmp/css_rules_analysis.md)
- CSS templates: [claude_code_log/templates/](../claude_code_log/templates/)
- Messages documentation: [messages.md](messages.md)

# Tool Coverage

> See [application_model.md](application_model.md) for the system overview,
> and [implementing-a-tool-renderer.md](implementing-a-tool-renderer.md) for
> the how-to when closing a gap listed here.

Which Claude Code tools get a specialized renderer, and which fall back
to the generic one. Checked against the upstream
[Tools reference](https://code.claude.com/docs/en/tools-reference)
(42 documented tools, snapshot 2026-07-15).

## Three levels of support

Coverage is not binary — a tool can be typed on one side and generic on
the other:

1. **Typed input** — an entry in `TOOL_INPUT_MODELS`
   (`factories/tool_factory.py`) parses `tool_use.input` into a Pydantic
   model, which `_dispatch_format` routes to `format_<Model>` /
   `title_<Model>` methods in `html/renderer.py` and `markdown/renderer.py`.
2. **Typed output** — an entry in `TOOL_OUTPUT_PARSERS` turns the raw
   `tool_result` (and, for members of `PARSERS_WITH_TOOL_USE_RESULT`, the
   structured `toolUseResult`) into a dataclass with its own formatters.
3. **Generic fallback** — no registry entry. Input renders as a
   `<table class="params">` key/value dump (`format_ToolUseContent`),
   output as a raw `<pre>` block (`format_ToolResultContent`), and the
   header shows the bare tool name, HTML-escaped
   (`title_ToolUseMessage`, see #245).

The fallback is a genuine feature, not a hole: it means an unknown tool —
a brand-new built-in, an `mcp__*` tool, a plugin's tool — always renders
something faithful. Typing a tool buys a compact title, structured
formatting, and correct Markdown/JSON export; it is worth doing for tools
that appear often or carry structure worth surfacing.

**JSON export needs no per-tool work.** `json/renderer.py` serialises
whatever the factory produced via `dataclasses.asdict`, so a tool's JSON
representation improves for free the moment it gets typed models.

## Documented tools

Legend: **Full** = typed input + typed output · **Input only** = typed
input, generic output · **Generic** = no registry entry, fallback
rendering.

| Tool | Support | Notes |
| :--- | :--- | :--- |
| `Agent` | Full | Aliased to `TaskInput` / `parse_task_output`; see [agents.md](agents.md) |
| `Artifact` | Full | #257 |
| `AskUserQuestion` | Full | Input card elides when the paired answer re-renders the questions |
| `Bash` | Full | `minted_background_task_id` hoisted from output (#158) |
| `CronCreate` | Full | |
| `CronDelete` | Full | |
| `CronList` | Full | |
| `Edit` | Full | |
| `EnterPlanMode` | Generic | |
| `EnterWorktree` | Generic | |
| `ExitPlanMode` | Full | |
| `ExitWorktree` | Generic | |
| `Glob` | Input only | `GlobOutput` model exists but no parser registers it — see "Dead models" below |
| `Grep` | Input only | |
| `ListMcpResourcesTool` | Generic | |
| `LSP` | Generic | |
| `Monitor` | Full | |
| `NotebookEdit` | Generic | |
| `PowerShell` | Generic | Windows sessions only; shape is close to `Bash` |
| `PushNotification` | Generic | |
| `Read` | Full | |
| `ReadMcpResourceTool` | Generic | |
| `RemoteTrigger` | Generic | |
| `ReportFindings` | Generic | Structured findings list — a good typing candidate |
| `ScheduleWakeup` | Full | |
| `SendMessage` | Full | See [teammates.md](teammates.md) |
| `SendUserFile` | Generic | |
| `ShareOnboardingGuide` | Generic | |
| `Skill` | Input only | Body folded in from the paired `isMeta` entry (#93) |
| `TaskCreate` | Full | |
| `TaskGet` | Generic | Only `TaskGet` of the `Task*` family is untyped |
| `TaskList` | Full | |
| `TaskOutput` | Full | Back-links to the spawning call (#90, #154) |
| `TaskStop` | Full | #158 follow-up |
| `TaskUpdate` | Full | |
| `TodoWrite` | Input only | Disabled by default upstream since v2.1.142, but ubiquitous in older transcripts |
| `ToolSearch` | Generic | |
| `WaitForMcpServers` | Generic | |
| `WebFetch` | Full | |
| `WebSearch` | Full | |
| `Workflow` | Input only | Input `script` is JS; #174 |
| `Write` | Full | |

**Totals:** 21 full · 5 input only · 16 generic.

## Tools we support that upstream no longer documents

A transcript viewer reads *history*. These entries look obsolete against
today's reference but are load-bearing for archived transcripts, so
**none of them should be dropped** — a removal silently degrades old
logs to generic rendering.

| Tool | Why it's gone upstream | Why we keep it |
| :--- | :--- | :--- |
| `Task` | Renamed to `Agent` | Every subagent spawn in pre-rename transcripts |
| `MultiEdit` | Removed; superseded by `Edit` | Common in older transcripts; has input model + title, no output parser |
| `ask_user_question` | Legacy snake_case name of `AskUserQuestion` | Aliased to the same input model |
| `TeamCreate` | Never in the reference table | Agent-teams feature; fully typed both sides |
| `TeamDelete` | Never in the reference table | Ditto |

`TeamCreate` / `TeamDelete` are the inverse case: emitted by the
agent-teams feature but absent from the public table. Undocumented is
not the same as obsolete — the reference table is not a complete census
of what lands in a JSONL file.

## Dead models

`GlobOutput` and `GrepOutput` are declared in `models.py` and
`GlobOutput` even has a `format_GlobOutput` in `markdown/renderer.py`,
but neither is registered in `TOOL_OUTPUT_PARSERS`, so nothing ever
constructs them — `ToolOutput` carries an explicit
`# TODO: Add as parsers are implemented` for both. Either wire up the
parsers or drop the models; right now they read as coverage that isn't
there.

## MCP and plugin tools

`mcp__<server>__<tool>` names never appear in the reference table and are
unbounded by definition, so they always take the generic path. Plugins
can register their own formatters — see [plugins.md](plugins.md).

## Keeping this page current

The upstream reference changes with most Claude Code releases. When
revisiting: re-fetch the reference, diff its tool names against
`TOOL_INPUT_MODELS` / `TOOL_OUTPUT_PARSERS` keys, and move rows between
the two tables above rather than deleting them — a tool leaving the
reference means it moves to "no longer documented", never that support
gets removed.

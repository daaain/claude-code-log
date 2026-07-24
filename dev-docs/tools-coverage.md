# Tool Coverage

> See [application_model.md](application_model.md) for the system overview,
> and [implementing-a-tool-renderer.md](implementing-a-tool-renderer.md) for
> the how-to when closing a gap listed here.

Which tools get a specialized renderer, and which fall back to the generic
one. The first census covers Claude Code tools and the second covers Codex
item/tool adaptation.

The Claude Code table is checked against the upstream
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

## Tools we support that upstream does not document

A transcript viewer reads *history*, and provider adapters also need internal
canonical tools. These entries are historical, undocumented, or deliberately
internal, but all are load-bearing for supported transcripts; removing one
silently degrades rendering.

| Tool | Upstream status | Why we keep it |
| :--- | :--- | :--- |
| `Delete` | Internal canonical tool; never in the reference table | Codex `apply_patch` deletions receive a dedicated typed pair instead of masquerading as edits |
| `Task` | Renamed to `Agent` | Every subagent spawn in pre-rename transcripts |
| `MultiEdit` | Removed; superseded by `Edit` | Common in older transcripts; has input model + title, no output parser |
| `ToolExecution` | Internal canonical tool; never in the reference table | Faithful typed fallback for Codex JavaScript that the bounded analyzer cannot reconstruct safely |
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
unbounded by definition, so the default is faithful generic rendering. Plugins
can register their own formatters, and providers may canonicalize a small,
evidence-backed family before plugin dispatch. The Codex provider does this for
OpenAI Developer Docs search/fetch calls; all other MCP names remain open-ended.
See [plugins.md](plugins.md).

## Keeping this page current

The upstream reference changes with most Claude Code releases. When
revisiting: re-fetch the reference, diff its tool names against
`TOOL_INPUT_MODELS` / `TOOL_OUTPUT_PARSERS` keys, and move rows between
the two tables above rather than deleting them — a tool leaving the
reference means it moves to "no longer documented", never that support
gets removed.

## Codex provider coverage

Codex needs a separate census because its persisted rollout format is an
implementation detail, not a closed public list of function names. The public
[app-server](https://developers.openai.com/codex/app-server/) `ThreadItem`
union is the semantic reference; concrete function, MCP, app, plugin, and
collaboration tool names remain open-ended.

This snapshot was checked on 2026-07-19 against the official Codex manual and
the JSON schema generated by `codex-cli 0.144.3`. The generated schema contains
18 `ThreadItem` variants. Coverage below describes the rollout provider, not a
claim that rollout records use the app-server wire shape directly.

### Public item families

Legend:

- **Direct** — the provider normalizes the observed rollout family itself.
- **Adapted** — an observed rollout call is converted to an existing shared
  semantic renderer.
- **Partial** — useful observed shapes are covered, but the public item family
  is not decoded comprehensively.
- **Missing** — no semantic normalization exists yet; an unknown persisted
  record is ignored rather than guessed.

| App-server item | Coverage | Rollout-provider behavior |
| :--- | :--- | :--- |
| `userMessage` | Direct | Visible event/response copies are locally deduplicated; text, steering, environment context, shell commands, and inline image references are normalized. |
| `agentMessage` | Direct | Visible assistant text is normalized and ordered with other entries. |
| `plan` | Partial | Observed `update_plan` calls become `TodoWrite`; a native plan item shape is not decoded. |
| `reasoning` | Direct | Readable summaries render as thinking; encrypted reasoning is deliberately never inspected or emitted. |
| `commandExecution` | Adapted | `exec_command` becomes `Bash`; terminal `wait`/`write_stdin` polling and ordered or parallel marker sessions are coalesced when correlation is complete. |
| `fileChange` | Partial | Static `apply_patch` Add/Delete operations become `Write`/`Delete`; adjacent Update runs become `Edit`/`MultiEdit`, preserving patch order. Moves, dynamic patches, and ambiguous programs remain `ToolExecution`. |
| `mcpToolCall` | Adapted | Exact MCP names and forwarded result envelopes are preserved, allowing plugins such as ClMail to apply the same transformation as for Claude Code. Codex OpenAI Developer Docs searches and fetches receive built-in renderers. |
| `dynamicToolCall` | Partial | Direct open-ended calls render generically; statically analyzable `exec` wrappers expand, while opaque JavaScript remains `ToolExecution`. |
| `collabAgentToolCall` | Partial | Observed spawn/message/list function calls reuse `Task`, `SendMessage`, and `TaskList`; the native public item shape is not decoded directly. |
| `webSearch` | Adapted | Search-only `web__run` calls become `WebSearch`; exact open-only batches and find-only calls become `WebFetch`. Codex citation/source serialization is normalized before Markdown rendering. Mixed actions remain generic. |
| `imageView` | Partial | User-message image wrappers can inline readable local files. A native image-view item/tool result has no specialized adapter. |
| `imageGeneration` | Missing | No observed native rollout mapping is normalized. A function/MCP tool with this behavior remains generic unless a plugin transforms it. |
| `sleep` | Partial | Static Promise/`setTimeout` wrappers become synthetic `wait` pairs; a native sleep item shape is not decoded. |
| `subAgentActivity` | Partial | Thread lineage and spawn calls are retained, but `load_session()` does not yet splice descendant activity into the parent transcript. |
| `hookPrompt` | Missing | No observed rollout mapping is normalized. |
| `enteredReviewMode` | Missing | No observed rollout mapping is normalized. |
| `exitedReviewMode` | Missing | No observed rollout mapping is normalized. |
| `contextCompaction` | Missing | Exact persisted compaction shape and interrupted/delta recovery remain evidence gaps. |

**Totals:** 3 direct · 3 adapted · 7 partial · 5 missing.

### Concrete Codex call adapters

These mappings run before the shared tool factory, so a mapped name receives
the same typed models and HTML/Markdown/JSON rendering as a Claude Code tool.
Result transport is normalized in the provider before reaching shared
factories.

| Codex call | Canonical rendering | Coverage and fallback |
| :--- | :--- | :--- |
| `exec_command` | `Bash` | Typed input/output; approval justification becomes the description. Completed async polling chains fold into the originating Bash pair. |
| `apply_patch` | `Write` / `Delete` / `Edit` / `MultiEdit` | Lossless Adds and Deletes become individual `Write` and `Delete` pairs; adjacent static Updates reuse the edit renderers. One aggregate result is correlated to every derived pair. Otherwise `ToolExecution`. |
| `update_plan` | `TodoWrite` | Typed input; a successful empty transport becomes `Todo list updated.` |
| `spawn_agent` | `Task` | Typed input/output; opaque transport payloads are redacted on both specialized and `ToolExecution` paths. |
| `send_message`, `followup_task` | `SendMessage` | Typed input/output with target and follow-up semantics retained. |
| `list_agents` | `TaskList` | Typed input/output when agent rows are valid; malformed output stays generic. |
| search-only `web__run` | `WebSearch` | Typed input/output; a multi-query call keeps the first query in a compact title and lists every query in the body. Named `turn…search/view…` refs become anchors; numeric citation wrappers, word limits, and packed source-line markers are normalized into readable Markdown. |
| open-only `web__run` batch | `WebFetch` pairs | Expanded only when refs and result chunks split exactly; output uses the shared Codex web-result normalizer. |
| find-only `web__run` | `WebFetch` | Static refs and patterns become typed input; a reusable `turn…` ref links back to the card that introduced it, and output uses the shared Codex web-result normalizer. |
| `mcp__openaiDeveloperDocs__fetch_openai_doc` | `CodexDoc` | Codex-only built-in plugin: URL/anchor parameters stay visible and the returned documentation renders as collapsible Markdown. Aggregated static result objects are projected back into one pair per fetch. |
| `mcp__openaiDeveloperDocs__search_openai_docs` | `CodexDocSearch` | Codex-only built-in plugin: query/limit parameters stay visible; every surviving hit renders its linked hierarchy and Markdown content. Complete hit objects are recovered from otherwise-invalid truncated JSON. Aliased properties in aggregated static result objects are projected back into individual searches; properties removed by Codex output truncation remain explicit omitted results. |
| `mcp__*`, app, and plugin tools | Original name | Generic built-in rendering, followed by optional plugin transformation. The namespace is intentionally not closed. |
| static Promise delay | `wait` | Synthetic generic pair with `delay_ms` and an explicit completed result. |
| `wait`, `write_stdin` command polling | Originating `Bash` | Folded only with a matching live handle and terminal result; otherwise preserved as generic calls. |
| unknown direct function | Original name | Faithful generic params/result rendering. |
| unsupported `exec` JavaScript | `ToolExecution` | Original script remains visible without claiming native Workflow semantics. Results retain completion and wall-time status, followed by labelled generic result sections; opaque payloads are scrubbed. There is no legacy recognizer fallback. |

### `exec` JavaScript analysis

Codex often persists tool orchestration as JavaScript inside a custom `exec`
call. `providers/codex_quickjs.py` **executes** it in a sandboxed QuickJS
engine (`quickjs-ng`) with instrumented `tools` / `text()` stand-ins that
record what the snippet actually did; the recording is mapped back to a tool
batch. Because arguments are captured *after* the snippet's own JS evaluated
them, expression-built values (string `concat`, templates, `.join()`,
`reduce`, ternaries, computed numerics, conditional branches) resolve for free
— the fragile static-expansion whitelist of the former Tree-sitter analyzer is
gone.

Safety model: **no host callables** are registered (attack surface = the
QuickJS interpreter only). Per-snippet bounds — memory, wall-time, stack, a
pending-job cap, a 64 KB source cap, a 128 expanded-call cap, and a per-string
materialization cap — bound hostile inputs (infinite loops, allocation bombs,
deep recursion, string amplification). Provenance rides on an in-band
private-use sentinel (U+E000). Any failure — syntax error, engine exception,
a cap hit, a run that throws or never resolves, or a shape the mapper cannot
correlate — fails closed to `None`, so the raw-script `ToolExecution` fallback
stays visible.

The report → batch mapper recognizes:

- direct awaited calls, sequential batches, and heterogeneous `Promise.all()`
  batches with identifier or array destructuring;
- result provenance through direct references, property paths,
  `JSON.stringify()`, result-derived templates, and object emissions — both a
  whole-result **bundle** (`JSON.stringify({first, second})`, keyed per field)
  and a single-call **projection** (`{name, ...r}` spread, or explicit
  `{name, output: r.output}`), collapsed to the canonical `output` field;
- `for...of` expansion over static or computed arrays, including destructured
  rows and loop-local calls/emissions, and `Promise.all(array.map(async ...))`;
- ordered, reversed, and marker-delimited result correlation;
- `setTimeout` / Promise delays represented as `wait` calls;
- outer `exec` cell continuations coalesced before call expansion, including
  informational MCP completion events and collaboration bookkeeping inside the
  polling interval;
- command-session continuation through `wait` and `write_stdin`, including
  ordered and parallel marker sessions;
- consolidated output splitting on unique materialized template prefixes and
  projected JSON objects after a Codex truncation preamble; missing sections
  are identified only when Codex explicitly reports truncation.

Inter-call data dependencies (an unresolved result flowing into a later call's
argument), forged sentinels, ambiguous emissions, repeated/absent output
separators, `ALL_TOOLS`-registry pipelines (no static membership yet), and
cap/engine failures remain `ToolExecution`. False negatives are an acceptable
compatibility cost; false reconstruction is not.

### Refreshing the Codex census

The Codex manual states that generated app-server schemas match the installed
Codex version. To refresh this section:

```bash
codex --version
codex app-server generate-json-schema --out /tmp/codex-app-server-schema
```

Read the `definitions.ThreadItem.oneOf` variants from
`codex_app_server_protocol.v2.schemas.json`, compare them with the public-item
table, then reconcile the concrete adapter table against
`providers/codex_tools.py`, `providers/codex.py`, and the `test/test_codex_*`
contracts. Keep rollout observations explicitly separate from the public
app-server wire contract.

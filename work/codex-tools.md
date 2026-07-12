# Codex tool adaptation

> Branch: `dev/codex-tools`
> Baseline session: `019f4cc3-922e-7cc2-9975-cd529e8871af`, explored only
> through an isolated copy under `/tmp/codex-tools-home`.

## Goal

Reuse the existing typed tool models and specialized renderers when a Codex
call has the same semantics, while preserving the generic renderer as the
lossless fallback. Provider-specific raw shapes should not leak into the
factory or format-specific renderer layers.

## Boundary

`providers/codex_tools.py` adapts raw Codex names and inputs before the
provider constructs `ToolUseContent`. From that point onward the normal
pipeline applies unchanged:

```text
Codex raw call
  -> conservative provider adapter
  -> canonical ToolUseContent
  -> existing typed tool factory
  -> plugin transformers
  -> HTML / Markdown / JSON renderer
```

This ordering is important for MCP tools: an unwrapped
`mcp__clmail__communicate` remains generic to the built-in factory, then a
ClMail plugin can recognize and replace it exactly as it does for Claude Code.
No ClMail-specific rule belongs in the Codex provider.

## Exec wrappers

Current Codex rollouts persist most external calls as a
`custom_tool_call(name="exec")` containing generated JavaScript. The adapter
uses a deliberately small static decoder:

- exactly one lexical `tools.<name>({...})` call;
- comments and quoted strings are skipped when locating calls;
- the argument must be a JSON-compatible object literal with optional bare
  JavaScript property names and trailing commas;
- variables, expressions, malformed source, and multiple calls do not get
  guessed at.

A single recoverable call is unwrapped. Multi-call or dynamic programs become
the existing `Workflow` tool so their JavaScript remains visible. Unknown
direct tools retain their original name/input and use the generic renderer.

## Initial canonical mappings

| Codex call | Shared tool | Notes |
|---|---|---|
| `exec_command` | `Bash` | `cmd -> command`; approval justification becomes description |
| `spawn_agent` | `Task` | prompt/name retained; subagent type marked `codex` |
| `send_message`, `followup_task` | `SendMessage` | target/message mapped to recipient/content |
| `update_plan` | `TodoWrite` | steps become typed todo items |
| search-only `web__run` | `WebSearch` | mixed web operations remain generic |
| single `mcp__*` call | unchanged | enables plugin transformers |
| multi/dynamic `exec` | `Workflow` | preserves the orchestration source |
| everything else | unchanged | generic renderer |

`apply_patch` intentionally remains generic: forcing a free-form patch into
Claude's `Edit`/`MultiEdit` schema would invent file/old/new boundaries and
lose information. `wait`, `wait_agent`, `write_stdin`, and mixed `web__run`
calls similarly have no honest one-to-one mapping yet.

## Coverage on the isolated baseline

The copied session contains 221 exec wrappers and 46 direct collaboration or
wait calls. After adaptation:

- 95 `BashInput`;
- 71 `WorkflowToolInput` (multi-call or non-literal programs);
- 8 `SendMessageInput`;
- 7 `TodoWriteInput`;
- 6 `TaskInput`;
- 2 `WebSearchInput`;
- 78 generic calls, including MCP calls awaiting optional plugins.

The full copied session renders successfully to HTML. The adapter never reads
or writes the original rollout.

## Follow-ups

1. Exercise the real ClMail transformer package against unwrapped MCP calls.
2. Decide whether plugin transformers need access to provider/raw-call
   metadata in addition to the canonical `ToolUseMessage`.
3. Add result adapters only where Codex exposes stable structured output;
   otherwise keep the generic result body paired by `call_id`.
4. Sample newer sessions and app-server items for direct, non-wrapper tool
   shapes before expanding the mapping table.

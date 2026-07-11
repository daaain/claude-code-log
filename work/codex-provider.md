# Codex provider — investigation and implementation plan

> Status: research complete; implementation plan proposed 2026-07-10.
> Branch: `dev/codex`. Builds on PR #242 (provider abstraction) and
> PR #243 (AGY provider, currently at `c035fb3`).

This document is the coordination contract for adding Codex session support.
It records the evidence behind the design, separates verified facts from raw
format assumptions, and assigns parallel work by file ownership.

## 1. Goals and first usable slice

The first slice should:

1. Discover Codex rollout files from the configured Codex home.
2. Decode modern rollouts tolerantly, with limited legacy support.
3. Normalize messages, reasoning summaries, and tool calls/results into the
   existing transcript models.
4. Preserve enough thread and spawn identity to add subagent rendering without
   redesigning the provider later.
5. Provide an early manual-test command:

   ```bash
   uv run claude-code-log \
     --provider codex \
     --session-id <id-or-prefix> \
     -o /tmp/codex-session.html
   ```

The first slice does not need non-Claude cache integration, TUI browsing,
`--all-projects`, combined all-provider output, specialized rendering for every
Codex tool, or rendered subagent trees. Unsupported combinations must fail
clearly rather than falling through to Claude-specific paths.

## 2. Evidence and provenance

### PR #225

The superseded PR is archaeology, not code to cherry-pick. Its useful subset is
`claude_code_log/providers/codex.py`, the small synthetic Codex fixture, and the
Codex portion of `test/test_providers.py`.

Useful observations:

- session root under `~/.codex/sessions`;
- date-sharded `rollout-*.jsonl` files;
- `call_id` correlation for function calls and outputs.

Known defects:

- user messages are dropped;
- developer messages and visible assistant messages are misclassified as
  thinking;
- compaction, context, metadata, and most event variants are discarded;
- every emitted entry has `parentUuid=None`;
- session lookup uses ambiguous substring matching;
- `max_messages` is not actually forwarded;
- tests assert little beyond “some entries were returned”.

The PR's expanded documentation tree came from replacing repository symlinks
with copied content. None of those changes should be reused.

### Local observations

Observed against `codex-cli 0.144.1`; these are implementation details, not a
stable public protocol.

- Current rollouts use
  `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`.
- A legacy rollout was found directly under `sessions/`, so discovery must be
  recursive rather than assuming exactly three date components.
- Modern records use `{timestamp, type, payload}` envelopes.
- Observed envelope types include `session_meta`, `turn_context`, `event_msg`,
  `response_item`, `world_state`, and inter-agent metadata.
- Visible user and assistant text is usually duplicated between `event_msg`
  and `response_item`.
- Structured function calls, free-form custom calls, and MCP completion events
  have different shapes.
- `history.jsonl` is prompt history, not a complete session transcript.
- The versioned SQLite state database is useful as an index but must not be the
  only source of truth.

Fixtures derived from local data must be synthetic or fully sanitized. Never
copy prompts, outputs, instructions, encrypted reasoning, local paths, Git
remotes, connector data, account/rate-limit data, or real identifiers.

### Public semantic reference

The raw persisted rollout schema and filename layout are not publicly
documented as a compatibility contract. The supported app-server model is the
semantic reference:

- <https://developers.openai.com/codex/app-server/#items>
- <https://developers.openai.com/codex/app-server/#threads>

Its item taxonomy includes user/agent messages, plans, reasoning, command
execution, file changes, MCP/dynamic/collaboration tool calls, web searches,
image views, review-mode transitions, and context compaction.

The provider should therefore have three conceptual layers:

1. **Locator** — find active/archived rollout files and establish identity.
2. **Decoder** — parse raw version-drifting records without losing ordering.
3. **Normalizer** — map decoded records toward the public item semantics and
   then into `TranscriptEntry` models.

## 3. Storage, discovery, and identity contract

- Resolve `CODEX_HOME`; fall back to `~/.codex`.
- Search active `sessions/` recursively for supported rollout names.
- Account for `archived_sessions/` in the design; active-only support is
  acceptable in the first slice if stated and tested.
- Discover deterministically and build an exact `thread_id -> path` index.
- Never use substring-based file selection.
- For modern files, use the first `session_meta.payload.id` as the thread/file
  identity, with the filename UUID as a consistency check and fallback.
- Do not replace identity from a later `session_meta`: subagent rollouts may
  contain inherited parent metadata and history.
- Use metadata timestamps/cwd/model/version when available, with filesystem
  metadata only as a fallback.
- Count emitted normalized entries, not input lines, for `max_messages`.
- Malformed and unknown records are non-fatal. Warnings may include file and
  line number, but never record content.

## 4. Initial normalization rules

### Visible messages

- Prefer visible `event_msg.user_message` and `event_msg.agent_message`.
- Suppress their matching `response_item.message` duplicates.
- Use unmatched response messages as a fallback for interrupted or older
  rollouts, with conservative role handling.
- Do not render developer/system context as model thinking.
- Preserve source order across messages and tool records.

### Reasoning and compaction

- Render readable reasoning summaries when present.
- Never emit encrypted reasoning content.
- Represent compaction explicitly once a real sanitized raw shape is known;
  do not invent it from PR #225.

### Tools

- Pair calls and outputs by `call_id`.
- Support both structured `function_call` and free-form `custom_tool_call`
  families.
- Preserve string and content-array outputs.
- Normalize MCP/dynamic/collaboration calls structurally; tool names are open
  ended and must not form a closed enum.
- Let the existing generic tool renderer preserve unknown tools. Specialized
  Codex renderers can be added after the raw mappings are pinned by fixtures.

### Entry ordering

- Generate deterministic entry IDs from thread identity and source position or
  stable item ID.
- Build a linear `parentUuid` chain within each normalized thread initially.
- Treat `parentUuid` as render ordering, not as the sole representation of
  cross-thread lineage.

## 5. Codex subagents and the graph model

Codex does persist subagents as separate thread/rollout logs and provides
bidirectional linkage data.

Public app-server semantics expose:

- `collabToolCall` items with `senderThreadId`, `receiverThreadId`, and/or
  `newThreadId`;
- thread source kinds such as `subAgent`, `subAgentReview`,
  `subAgentCompact`, and `subAgentThreadSpawn`;
- `parentThreadId` and `ancestorThreadId` thread-list filters;
- `forkedFromId` for copied/forked history;
- descendant-aware archive/delete behavior.

Locally observed child rollouts also contain first-record metadata such as
`source.subagent.thread_spawn`, `parent_thread_id`, and `forked_from_id`.
Their own first `session_meta.id` matches the child filename, while
`session_id` may point to the parent. Child files can contain a copied parent
history prefix before unique child activity.

The resulting structure has two distinct levels:

1. **Within a thread:** an ordered sequence/tree of normalized items.
2. **Between threads:** a spawn/fork ownership tree, plus possible
   collaboration-message edges between already existing threads.

So “DAG of messages” remains a useful renderer shorthand, but the durable
model is more accurately a thread tree with cross-thread edges. The spawn
ancestry is tree-shaped; collaboration links can make the full relation a
directed graph.

Design constraints for the first slice, even before rendering children:

- Keep thread identity, parent thread identity, fork origin, source kind, and
  spawning call/item ID in an intermediate normalized representation.
- Do not flatten those relations irreversibly into `parentUuid`.
- Use the first child metadata record for lineage; do not infer subagent status
  from filenames.
- Identify and suppress inherited parent-history prefixes before rendering a
  child transcript.
- Preserve enough data to map a child later onto the existing Claude-side
  conventions:
  - child entries: `agentId=<child-thread-id>`, `isSidechain=True`;
  - render session: `<root>#agent-<child-thread-id>`;
  - spawning parent item: `spawnedAgentId=<child-thread-id>`.
- Attach the child block at the spawning collaboration tool item and, when
  available, relate its completion to the corresponding parent-side result.

This should reuse the depth-agnostic hierarchy work documented in
[`agent-hierarchies-design.md`](agent-hierarchies-design.md), not create a
parallel Codex-only tree renderer.

Subagent rendering can remain a follow-up, but the locator/decoder must retain
these fields and fixtures must cover at least one parent/child pair now.

## 6. Developer-message documentation layout

Move the existing Claude Code examples mechanically:

```text
dev-docs/messages/{assistant,system,tools,user}/
    -> dev-docs/messages/claude-code/{assistant,system,tools,user}/
```

Use `git mv` so example contents remain byte-identical and Git records them as
renames. Update path references and extraction scripts in a separate, focused
part of the same change.

Add provider-specific Codex material:

```text
dev-docs/messages/codex/
  README.md
  session/
  messages/
  reasoning/
  tools/
  lifecycle/
  collaboration/
  legacy/
```

The Codex README records producing CLI version, provenance, sanitization rules,
known gaps, and duplicate-record behavior.

## 7. Parallel implementation workflow

### Worktree isolation

Codex subagents share the invoking checkout by default. Implementation agents
must instead use dedicated sibling worktrees so their edits, tests, and commits
remain isolated. Existing user-managed worktrees (`main`, `alice`, `bob`,
`carol`, `dave`, `monk`, and this `codex` coordinator) are out of scope and
must not be modified.

Create new worktrees only after the coordination contract is present in their
common base commit. Use the required `codex-` prefix, for example:

```text
codex-provider-core   dev/codex/provider-core
codex-cli-rendering   dev/codex/cli-rendering
codex-fixtures-docs   dev/codex/fixtures-docs
```

The current `codex` worktree remains the integration worktree. Each agent
commits only on its assigned branch; the coordinator reviews and integrates
those commits sequentially, then runs the combined verification suite. Do not
remove any worktree until its commits are integrated and the user agrees that
cleanup is appropriate.

### Preparation

1. Land the rename-only message-doc move and path-reference updates.
2. Add sanitized contract fixtures, including a parent/child thread pair.
3. Pin expected normalized events before parser implementation diverges.

### Parallel wave

Each agent owns disjoint files unless the coordinator explicitly reassigns
them.

**Provider agent**

- `claude_code_log/providers/codex.py`
- Codex registration in `providers/registry.py`
- locator, decoder, normalizer, and provider unit helpers

**CLI/rendering agent**

- `claude_code_log/cli.py`
- provider-neutral render-from-entries helper in `converter.py`
- focused CLI tests

**Fixtures/tests/docs agent**

- `test/test_codex_provider.py`
- `test/fixtures/codex/`
- `dev-docs/messages/{claude-code,codex}/`
- path-reference/extractor updates for the documentation move

**Coordinator**

- owns shared model/base/DAG changes;
- freezes interfaces before parallel edits;
- reviews privacy, unknown-record behavior, and cross-agent integration;
- integrates and resolves conflicts.

## 8. Verification and acceptance criteria

- Pyright: zero errors and warnings.
- Existing unit suite remains green.
- Discovery tests cover date-sharded, flat legacy, archived/unsupported policy,
  invalid IDs, deterministic ordering, and duplicate IDs.
- Parser tests assert exact roles/content, source order, parent threading,
  call/result correlation, metadata, limits, malformed final lines, and unknown
  records.
- Duplicate visible message pairs render once.
- Encrypted reasoning and private metadata never appear in fixtures or warnings.
- A child rollout is identified correctly and its inherited parent prefix is
  distinguishable from unique child activity, even if not rendered yet.
- The manual CLI path renders an actual local session to HTML/Markdown/JSON
  without writing into `$CODEX_HOME` or exposing content in command output.

## 9. Follow-ups and open evidence gaps

- Exact raw compaction representation.
- Interrupted-turn/delta recovery rules.
- Image and local-image raw item shapes.
- Robust inherited-prefix boundary detection for subagents across versions.
- Full subagent block splicing and cross-agent communication rendering.
- Cache, TUI, all-session, and all-provider integration.
- Specialized formatting for common Codex tools.
- App-server `thread/read` as a compatibility oracle or future supported
  backend.
- Broader fixtures across Codex CLI versions and surfaces.

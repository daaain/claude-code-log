# Codex provider backlog

> Status snapshot: 2026-07-19
>
> This is a grouped inventory, not a committed implementation plan. Completed
> handoffs and design plans remain available in Git history.

## Delivered baseline

- Active rollout discovery under `$CODEX_HOME/sessions`, including current
  date shards and the supported flat legacy layout.
- Exact session lookup, duplicate-ID rejection, tolerant/private decoding,
  deterministic normalized IDs and parent chaining, and strict normalized
  message limits.
- Visible user/assistant messages, reasoning summaries, environment context,
  user shell records, local image inlining, and adjacent duplicate handling.
- Provider-local tool/result transport normalization with typed Bash, Write,
  Delete, Edit, MultiEdit, TodoWrite, Task, SendMessage, TaskList, WebSearch, and
  WebFetch reuse. Multi-query web searches use compact titles and explicit
  query lists; Codex web citations and source metadata become readable
  Markdown.
- OpenAI Developer Docs searches and fetches render through a Codex-only
  built-in plugin. Batched/aliased results project back to their source calls;
  complete search hits survive damaged truncation envelopes with linked
  hierarchy titles and Markdown bodies. Other MCP/plugin names remain generic
  and transformable by external plugins.
- Conservative async folding for outer code-mode cells and ordered or parallel
  command `wait`/`write_stdin` continuations.
- Tree-sitter-only bounded JavaScript analysis for static calls, batches,
  constants/templates/joins, loops/destructuring, delays, provenance, and
  truncated prefixed/projected output, including projection of aggregated
  static result objects back to their source calls, plus static-array `map`
  batches with spread result envelopes. Unsupported programs remain a typed
  `ToolExecution` pair with labelled results and preserved wall time; the legacy
  recognizer is not a production fallback.
- Cross-provider contracts, adversarial correlation/privacy tests, a sanitized
  schema corpus, and real provider-to-HTML/Markdown export tests.

The detailed current coverage census is
[`dev-docs/tools-coverage.md`](../dev-docs/tools-coverage.md); persisted sample
families and the provider contract are in
[`dev-docs/messages/codex/README.md`](../dev-docs/messages/codex/README.md).

## Semantic and evidence gaps

- Pin the exact persisted representation of the partially covered or missing
  app-server families: `plan`, `collabAgentToolCall`, `imageView`,
  `imageGeneration`, `sleep`, `subAgentActivity`, `hookPrompt`,
  `enteredReviewMode`, `exitedReviewMode`, and `contextCompaction`.
- Define interrupted-turn and delta recovery rules rather than relying only on
  completed response records.
- Expand the sanitized corpus across Codex CLI versions and surfaces. Keep the
  public app-server item schema as the semantic oracle, not as an assumption
  about rollout wire shape.
- Recheck inherited-parent-prefix boundaries across newer subagent rollout
  variants and retain stable spawn/fork evidence when it appears.
- Evaluate app-server `thread/read` as a compatibility oracle or a future
  supported input backend; it should not silently replace local rollout
  support.

## Product integration gaps

- Discover or explicitly expose `archived_sessions`; the current provider is
  active-session-only.
- Add Codex sessions to cache, TUI, all-session/all-project, and combined
  multi-provider workflows. Today the supported public path is exact
  `--provider codex --session-id ...` export.
- Render the full Codex subagent thread tree under spawn calls, including
  nested descendants and cross-agent communication. Current discovery retains
  lineage and strips inherited history but `load_session()` emits one thread.
- Decide how native image-view results should render, independently of the
  already-supported user-message image references.
- Codex token accounting → index totals. The wholesale walker emits zero
  input/output/cache token totals per project because Codex rollouts carry no
  token accounting the provider currently surfaces; the index token summary is
  therefore always blank for Codex projects. If/when token counts are extracted
  from rollout records, thread them into the walker's project summaries so the
  index totals populate like the Claude path.

## Tool and static-analysis candidates

- Add semantic mappings only for recurring Codex calls with an honest shared
  equivalent; unknown direct calls and MCP/plugin namespaces should stay
  generic by default.
- Sample additional direct non-wrapper tool shapes and newer generated
  JavaScript before expanding the whitelist.
- Candidate abstract-interpreter additions include safe constant member/index
  evaluation (result-field provenance is already covered), more immutable
  expression operators, and additional bounded control flow beyond the current
  static loops and session-marker condition. Each addition needs both positive
  provenance tests and negative ambiguity/mutation tests.
- Keep consolidated-output recovery conservative: split only on unique static
  materialized boundaries, and never invent a missing result unless Codex
  explicitly reports truncation.

## Internal architecture debt

These refactors were intentionally deferred until behavior was pinned:

1. **Split provider responsibilities.** Extract rollout catalog, tolerant
   decoder, reconstruction passes, and transcript normalizer; keep
   `CodexProvider` as a thin orchestration facade.
2. **Remove repeated rollout scans.** Cache identities/decoded records within
   one discovery/load operation and add an operation-count test proving
   constrained/linear behavior without timing flakes.
3. **Simplify registry/discovery ownership.** Choose one discovery facade,
   validate factories/instances, and remove or exercise unused provider hooks.
4. **Centralize entry construction.** Introduce a context/builder for session,
   model, cwd, version, timestamp, and parent chaining; widen result helpers
   only with cross-provider structured/error regressions.

Architecture work must preserve the provider contract, Codex adversarial
suite, and HTML/Markdown exports after every extraction. Avoid long-lived cache
state until invalidation semantics are explicit.

## Refresh checkpoints

- When Codex is upgraded, generate its app-server JSON schema and compare the
  18-item snapshot documented in `dev-docs/tools-coverage.md`.
- Keep fixtures synthetic and privacy-scanned; never commit real rollouts or
  generated session HTML.
- Before publication, run `just ci`, then smoke-render the canonical local
  session if it is still available. Do not push merely because a local commit
  was created.

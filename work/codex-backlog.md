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
- Cache-backed load + paginated combined for the wholesale walker. v1
  participates in the SQLite cache for render-SKIP only: `render_provider_wholesale`
  populates the messages table via `save_cached_entries` for schema uniformity
  but never LOADS from it — every run re-parses rollouts (cheap, and it avoids a
  serialization round-trip fidelity risk). Flip to cache-backed load once the
  round-trip is proven byte-stable for Codex entries. The combined page is also
  a single unpaginated document; wire `_generate_paginated_html` (cache-coupled)
  and flip `--page-size`/`--jobs` from loud-rejected to honored at the same time.
- Unify the spawn_agent/Task scrub policy. There is a clean-vs-laundered seam:
  a cleanly-correlated single `spawn_agent` renders as a Task tool with its
  message shown, while a laundered/unrelated-emission one is kept on the
  scrubbed-opaque ToolExecution fallback (single-call widening excludes
  Workflow-family). That makes the scrub boundary depend on JS shape, not
  content — an artifact, not a defensible privacy contract. Decide one policy:
  shown-everywhere with opaque-literal scrubbing of the canonicalized inputs, or
  scrubbed-everywhere. cboos's ruling to take later.

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
- Laundered-row cross-read hole in the interleaving relaxation — uniqueness
  tightening. `_relax_interleaved` attributes a provenance-laundered row to a
  call by execution slot and checks reads only GLOBALLY (every call read
  somewhere), so a row that actually read a different call could be
  mis-attributed if its slot-call is satisfied by a dead read. The membership
  fix (slot-call must be read in that row's window) is defeated because the dead
  read lands in-window. The unbeaten variant: scope reads per emission window
  (the same `after`-counter mechanism used for texts) and require the window's
  DISTINCT read-set to be exactly `{slot call}` — a laundered row that reads two
  calls in its window (one dead) is then non-unique and fails closed, while a
  legitimate laundered row reads exactly its own call. Likely zero-regression on
  the recovered set but unmeasurable without building it; the hole needs dead
  code to reach, so it is documented (see the `_relax_interleaved` docstring)
  rather than guarded for now.
- Prelude instrumentation integrity — the correlation bookkeeping is
  snippet-writable. `_PRELUDE_TEMPLATE` declares `__records`, `__texts`,
  `__errors`, and `__reads` as plain `globalThis` arrays, and the instrumentation
  hooks (`__noteRead` and the tool/text recorders) push into them directly. An
  analyzed snippet can therefore write these globals itself: forged
  `__records`/`__texts` entries fabricate tool calls or emitted rows in the
  rendered transcript, and forged `__reads` entries influence the read-gated
  attribution in `_relax_interleaved`. This is a different class from the
  cross-read hole above — that is an attribution ambiguity reachable only by dead
  code with no hostile intent; this is direct hostile writes to the bookkeeping.
  The fix is uniform, not per-array: move all four behind a closure and expose
  only a single non-enumerable, non-writable extraction hook that returns a
  detached snapshot — serialized data, or a deep copy / frozen structure — and
  never a live reference to the closure-owned arrays. The descriptor flags seal
  only the binding, not the array a hook hands back: returning the live arrays
  would let a snippet call the hook and push forged entries straight into them,
  reintroducing the same forgery the closure was meant to prevent. Hardening
  `__reads` alone would shut the narrow door while leaving the wider
  `__records`/`__texts` forgery open, so piecemeal hardening is not worth doing. Threat model that
  bounds the priority: the snippet originates in the user's own transcript, and
  the QuickJS sandbox and evaluation caps hold, so the worst outcome is
  misleading rendered output for the user viewing their own session — not sandbox
  escape, code execution, or data exfiltration. Deferred on that bound; revisit
  if the analyzer ever runs on untrusted third-party rollouts or the extraction
  surface grows.

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

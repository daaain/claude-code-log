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
- **Separate "malformed session id" from "session not found".**
  `_SESSION_ID_RE` is `[A-Za-z0-9_-]+`, a *character-set* filter and not a UUID
  validator: `abc`, `1234` and `not-a-uuid` all `fullmatch`. So an id that could
  never name a session passes validation and only surfaces further down as
  `FileNotFoundError` from the index lookup. A caller needing to distinguish
  *you typed nonsense* from *that session is not here* currently cannot, and the
  two deserve different messages. **This is a behaviour change at a boundary —
  an exception type callers may already branch on — not a tidy-up**, which is
  why it was held out of the decode work rather than folded in.

  The same pattern and the same use exist in **both** providers
  (`providers/codex.py:57`, used at `:555` and `:599`;
  `providers/claude.py:12`, used at `:49`), so a fix has to cover both or state
  why they should differ — changing only the Codex copy would leave the defect
  live *and* make the two providers disagree about what a bad id does. Note the
  Codex side now has **two** validation call sites rather than one, so "fix the
  provider" means both of them.

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
- Index token-display gaps (both providers, pre-existing — two faces of one
  thing). Current parity: per-session token totals are CACHED for both formats,
  but project totals are DISPLAYED on the HTML index only. Not a Codex-specific
  hole.
  1. **Per-session token ROWS on the HTML index.** The session-nav macro already
     renders `{% if summary.token_summary %}` (`index.html:45-48`) and the cache
     has stored per-session token totals since the initial schema on BOTH paths
     — the key is simply never populated into the project-summary session dicts,
     so no index shows per-session token rows today. Feeding it is small and
     needs no template work, but it is a CLAUDE-path UX change (every user's
     cards gain per-session rows) that intentionally updates the Claude index
     snapshot — its own PR, where that snapshot delta is the reviewable point,
     NOT bundled into Codex token accounting (which keeps its "0 `.ambr`
     changes" byte-stability signal). The drift pin
     `test_index_summary_dict_shape_matches_claude_path` stays green because
     both paths gain the key together.
  2. **Markdown index token display (a feature, not a fix).** `token_summary` is
     consumed only by the HTML index template (`index.html:45-48`, `:90-91`);
     the Markdown projects-index emits project/session/message counts only, so
     project token totals never render on the MD index — for Codex OR Claude.
     The surface has never existed in the Markdown renderer. This matters for
     the vault/Obsidian workflow specifically: `--expand-paths` defaults
     `--combined=no` for Obsidian use, so anyone rendering a vault in Markdown
     gets no token totals at all.
- Codex per-turn / per-message token accounting. Session and project token
  totals now populate the wholesale index (project cards; per-session totals
  are stored on the session cache — parity with the Claude schema — pending the
  per-session-row display item above),
  extracted from the LAST cumulative `token_count` record per session — see
  `providers/base.ProviderTokenTotals`, `codex._token_totals_from_records` /
  `_map_cumulative_usage`, and the threading in `render_provider_wholesale`.
  What remains deferred is FINER-grained attribution (per-turn, per-message).
  It is an evidenced design limit, not a TODO: Codex emits a `token_count`
  after nearly every agent-loop step — measured over the real corpus (n=4138
  events, 34 sessions) ~75.6% follow a tool-execution step and only ~22.5%
  follow an assistant/agent message — so the per-step delta (`last_token_usage`)
  spans reasoning + tool I/O + the next turn's cached re-read and does not slice
  onto the messages the transcript renders. The session cumulative is the
  finest honest unit. Do NOT "fix" this into per-message numbers without a way
  to attribute a delta to exactly one rendered message; the impossibility is
  documented at `codex._token_totals_from_records` and in
  `dev-docs/tools-coverage.md`.
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
- Standalone user-docs page for provider wholesale + Obsidian output. The
  `--expand-paths` / `--filter-path` / `--combined` interactions, synthetic
  group-by-cwd projects, and `--clear-output` stale-sweep are currently surfaced
  as a feature-list bullet plus the live-generated CLI reference. If the surface
  keeps growing, a dedicated how-to page (flat vs expanded layout, migration)
  may be worth carving out — kept out of the flags/labels PR to avoid widening
  its diff with a structural docs decision.

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
2. **Remove repeated rollout scans. — DONE (PR #302).** Both halves landed: the
   totals seam (returning entries and totals from one decode) and the
   fork-prefix fan-out (reusing the identity discovery already computed, and
   decoding a shared parent once per discovery rather than once per child).

   Measured over a frozen 34-rollout archive, 158.6 MB of source, page cache
   pre-warmed, `use_cache=False`:

   | | before | after |
   |---|---|---|
   | `_decode_records` calls | 236 | **104** |
   | per-path factor (34-path floor) | 6.94x | **3.06x** |
   | bytes actually parsed | 798.4 MB | **327.8 MB** |
   | render peak (tracemalloc) | 727.3 MB | **641.8 MB** |
   | retained identity map | — | **+20 KB** (725 B × 34) |

   Across the whole arc, including the two-call walker that preceded the seam
   fix, parsed bytes fall **1276.6 → 327.8 MB**. Phase split: discovery
   118 → 70, load 118 → 34.

   **Read the hottest-file figures with their labels.** The 11.6 MB parent of 12
   forks — the file that *was* worst — falls **28x → 3x**. The *new* maximum is
   **4x**, held by rollouts that are both a fork child and a shared parent. Both
   count every `_decode_records` call, header reads included; the earlier "3x as
   hottest" was one file tracked across the change while the superlative moved.

   **The operation-count test this item asked for now exists**:
   `test/test_codex_fork_prefix_decodes.py`, fully synthetic (`tmp_path` only,
   no reach into a real data dir), counting the primitive rather than timing
   anything. It pins per-path decode budgets as *exact* equalities — two-sided
   on purpose, since a count that is too **low** means a child's prefix
   comparison never ran — plus k-invariance of a shared parent's cost, the
   ambiguity contract, and the absent-vs-zero case below.

   **What genuinely remains, stated as unmeasured.** The per-file ceiling is
   **4** and it is structural, not redundant: a rollout is decoded once as a
   fork child and once as a shared parent when it is both, plus one header read
   and one decode for its own load. Going lower needs either candidate lists
   retained across parent groups — the retention rejected below on measurement —
   or a **bounded tail read** of the parent, since the prefix comparison only
   needs the parent's tail. **Neither is measured; do not quote a figure for
   either.**

   **Why the cache shape was rejected**, and it still constrains any future
   attempt: unbounded, a decoded-record cache holds **264 MB resident for
   152 MB of source**, and an entry-count LRU cannot bound it because a single
   rollout decodes to **124 MB**. Any cache here needs a byte budget with
   explicit eviction. What landed instead retains only identities — a fixed
   handful of scalars and two `Path`s each — so bounding the entry *count*
   bounds the memory. **Entry size is the discriminator, not lifetime**: the map
   lives for the whole run, exactly as the path index already did.

   Two invariants that fail *silently* if a later change disturbs them:
   a duplicated thread id is **retained** by discovery but **illegal** to load,
   so no fast path may skip the ambiguity raise; and
   `inherited_prefix_records == 0` is indistinguishable from *not computed* if
   membership is inferred from the value — and 0 is the common case, so
   presence-in-the-resolved-map is what signals "computed".

   **This is resource use, not wall clock.** Describe it as amplification, never
   as a speedup: rendering dominates the run, so removing decodes buys I/O and
   CPU rather than a visible improvement. An earlier profiling pass put roughly
   70s of a ~93s instrumented run in HTML/Markdown generation, with interleaved
   medians of 54.6s vs 56.4s — quoted as that run's numbers, not re-derived here,
   and deliberately not the justification for anything. Wall clock on this work
   has already produced one retraction: a +40.7% regression that did not
   reproduce (+9.9% controlled) because the harness was launched on a box under
   load. **Timing figures here need an idle machine and interleaved arms, or they
   measure the machine.** The deterministic figures above carry the case on their
   own.

   **METRIC CAVEAT — the byte figures published before 2026-07-31 over-charge by
   1.59x, and the error flatters this exact fix.** `Σ(file size × decode calls)`
   bills every call as a full file read, but `_decode_records` streams lazily
   from an open handle and `_read_identity` returns on the first `session_meta`,
   so a one-line read was charged a whole file:

       CHARGED   Σ(size × calls)        1272.5 MB   8.0x
       CONSUMED  lines actually parsed   798.4 MB   5.0x
       over-charge                                  1.59x

   It closes to the byte: **102 of those 236 calls are early exits** (34 each at
   `_session_index`, `_discover_in`, `_load_in`), so the phantom charge is
   3 × 158.6 = **475.8 MB** against an observed charged−consumed gap of
   **474.1 MB**, leaving 1.7 MB as what those calls really read. The over-charge
   is **not a fixed discount** — it measured 1.495x / 1.594x / 1.482x across the
   three states — so charged bytes cannot be converted to parsed bytes by any
   constant, and two figures are comparable only within one metric.

   The consequence for scoping anything here: collapsing the 3N `_read_identity`
   calls to N scores **~317 MB charged and ~1.1 MB real**. Report **parsed** as
   the honest number; keep charged only to compare against older charged
   figures. Where the bytes actually were: decoding each parent once per child
   cost **277.5 MB**, against **41.6 MB** for all 25 fork children put together,
   and the 11 *distinct* parents are only **127.1 MB**. That is why grouping by
   parent was the byte win independently of any call count. (Those two sum to
   319.1 MB against a measured discovery total of 320.2 MB, the 1.1 MB remainder
   being the 34 early-exit header reads — which is also an independent check that
   the phase attribution is right.)

   **The measurement METHOD, recorded because the corpus is not being kept.**
   The 152 MB archive these figures came from is real user data with no owner and
   is being retired; the synthetic test above is its successor for *regression*,
   but it cannot reproduce these *magnitudes*. To re-measure credibly:

   - Freeze the input first and hash it. Comparing against a live archive makes
     drift indistinguishable from regression. Hash file *contents* in sorted-name
     order, not `md5sum` output — that embeds paths, so a byte-identical tree at
     a different path yields a different digest and looks exactly like drift.
   - Attach at `_decode_records` — the single primitive every path funnels
     through — and count per path, tagging the discovery and load phases
     separately. The phase split is what localises a regression.
   - Run both arms **in one process** with the page cache pre-warmed, and
     `use_cache=False` so a render-skip cannot mask a decode. Interleave arms
     rather than sequencing them: on a loaded machine, sequenced arms measure the
     machine — a sequenced run once produced a stable-looking +50% for the
     *faster* arm while call counts were byte-identical.
   - For parsed bytes, count the lines actually handed to the parser (a counting
     proxy around the file object works, and avoids re-implementing the decoder
     — a lookalike decoder that disagrees is indistinguishable from a
     regression). Do **not** infer bytes from call counts.
   - Anchor memory claims on the **max** decoded size, not a median or a ratio:
     the max (124 MB) reproduces to the digit, while the median wobbles ~0.9–1.05
     MB with allocator overhead, so any max/median ratio is not reproducible.
     One earlier "~120x" figure was retracted for exactly this.
   - Treat the figures above as a **historical snapshot of one archive**, not a
     target. A different corpus with different fork density will not reproduce
     them, and should not be expected to.

   The 127-line measurement analysis on the unpushed `dev/codex-decode-backlog`
   branch is **superseded by this item** — it predates the metric correction and
   states the charged figures as bytes read. Do not merge it as-is.
3. **Simplify registry/discovery ownership.** Choose one discovery facade,
   validate factories/instances, and remove or exercise unused provider hooks.
4. **Centralize entry construction.** Introduce a context/builder for session,
   model, cwd, version, timestamp, and parent chaining; widen result helpers
   only with cross-provider structured/error regressions.

Architecture work must preserve the provider contract, Codex adversarial
suite, and HTML/Markdown exports after every extraction. Avoid long-lived cache
state until invalidation semantics are explicit — noting that item 2 landed a
run-lifetime *identity* map under that rule rather than as an exception to it:
it shares the key, lifetime and staleness assumption of the path index that was
already there, and its entries are a fixed size, so bounding the count bounds
the memory. The rule still forbids what it was written to forbid, which is
retaining decoded **records**.

## Refresh checkpoints

- When Codex is upgraded, generate its app-server JSON schema and compare the
  18-item snapshot documented in `dev-docs/tools-coverage.md`.
- Keep fixtures synthetic and privacy-scanned; never commit real rollouts or
  generated session HTML.
- Before publication, run `just ci`, then smoke-render the canonical local
  session if it is still available. Do not push merely because a local commit
  was created.

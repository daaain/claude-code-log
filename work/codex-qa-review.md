# Codex provider QA review and hardening plan

> Review date: 2026-07-13
> Branch: `dev/codex-tools`
> Scope: the complete `main...HEAD` provider arc, including the provider
> abstraction/AGY baseline, Codex discovery and normalization, single-session
> CLI export, specialized tool/message adaptation, shared factories, renderers,
> fixtures, and documentation.

## 1. Review method and current evidence

The arc was reviewed from three independent perspectives:

1. adversarial format/privacy/correlation review;
2. QA and regression-coverage review;
3. code-quality, reuse, and architecture review.

The coordinator also inspected the full diff, ran focused tests, collected
serial coverage, ran full pyright, and rendered the real `019f4cc3...` rollout
during the preceding implementation round.

Current evidence:

- 61 focused Codex tests pass;
- full pyright reports zero errors;
- focused coverage is 87.9% for `providers/codex.py`, 87.7% for
  `providers/codex_tools.py`, and 93.8% for `providers/codex_messages.py`;
- provider registry focused coverage is only 26.4%;
- the focused coverage run has no direct coverage of ClaudeProvider or
  AgyProvider;
- targeted ruff finds two import-order errors in `providers/agy.py`;
- the worktree contains one unrelated generated HTML file, which must remain
  untracked and must not be committed.

High line coverage in the Codex modules does **not** close the review: the main
risks are semantic correlations and conservative fallbacks, where a happy-path
line can be covered without testing the unsafe alternative.

## 2. Release-blocking findings

### R1 — Workflow fallback can expose opaque agent payloads

`providers/codex_tools.py` redacts Fernet-shaped `spawn_agent.message` only
after a wrapper has been accepted as a simple canonical call. A compound or
malformed wrapper falls back to `Workflow` with the original JavaScript,
including the opaque payload.

Required outcome:

- apply one privacy scrub before every renderable fallback path;
- preserve script structure while replacing opaque string values;
- cover compound, malformed, dynamic, and multi-emission wrappers;
- verify ordinary long prompts remain unchanged.

### R2 — Visible-message deduplication is session-global and content-only

`CodexProvider` pre-counts every visible event fingerprint and suppresses
matching response messages anywhere in the session. Repeated legitimate text
in separate turns can therefore suppress the wrong, earlier message.

Required outcome:

- replace the session-global `Counter` pairing with order-local pairing;
- use adjacent/nearby corresponding event/response records, turn boundaries,
  timestamps, or stable IDs when present;
- preserve identical messages repeated in different turns;
- test event-first, response-first, interrupted, repeated-text, and unmatched
  fallback cases.

### R3 — Async Bash folding can cross visible activity and hide live handles

`_coalesce_command_sessions` defines adjacency using only tool records. A
visible user, assistant, or reasoning item between Bash and `wait` is ignored,
so later output can be moved backward and polling cards suppressed. A
non-terminal partial chain can also be hidden once it has emitted any chunk.

Required outcome:

- permit only explicitly invisible bookkeeping records between chain members;
- break on visible event/response content and unrelated tools;
- suppress continuation cards only after a terminal result (`exit_code`) is
  observed;
- retain incomplete/live session handles losslessly;
- add negative cases for mismatched cell/session IDs, intervening visible
  records, malformed envelopes, empty chunks, and truncated rollouts.

### R4 — Inherited-prefix stripping can delete genuine child activity

The current longest-match search accepts a child prefix found at any parent
offset, including a one-record coincidence. A common child-local message such
as `Done.` may be removed if it appears anywhere in the parent.

Required outcome:

- constrain matching to a defensible fork boundary/parent suffix and stable
  item identity where available;
- do not strip weak one-record or partial coincidences without stronger
  lineage evidence;
- assert loaded child content, not just discovery metadata;
- test coincidental matches, partial matches, interleaved metadata, missing or
  duplicated parents, and timestamp-only differences.

### R5 — Exec-wrapper recognition is not lexically safe enough

The forwarder predicate can find an assigned variable name inside a string or
comment (`text("result")`), while object-key rewriting can mutate text inside a
quoted command such as `echo {foo: bar}`. Both lead to false specialization or
false Workflow fallback.

Required outcome:

- make assignment, output-forwarding, and object-key parsing use the same
  literal/comment-aware scanner;
- require the single emission expression to structurally reference the
  assigned result (`result` or an allowed member such as `result.output`);
- never rewrite object keys inside strings/comments;
- add table/property tests for literals, comments, escapes, template strings,
  nested delimiters, trailing commas, and unterminated input;
- preserve the invariant: unsupported source never raises and never receives a
  specialized label.

### R6 — Codex transport rules leak into shared factories

Shared `factories/tool_factory.py` currently contains Codex-specific handling
for Task acknowledgement JSON, WebSearch fallback Markdown, and TodoWrite exec
envelopes. These rules can change legitimate Claude or future-provider output.

Required outcome:

- move transport decoding to a Codex result adapter at the provider boundary;
- pass canonical content/models into the existing provider-neutral factory;
- keep shared parsing only for truly shared semantic shapes;
- add Claude-shaped HTML and Markdown regressions before moving behavior;
- specifically protect successful/error Task reports, JSON containing
  `task_name`, WebSearch plain text/Markdown/error results, long queries, and
  TodoWrite results.

### R7 — Tolerant JSON decoding does not catch all parser failures

`_decode_records` catches `JSONDecodeError`, but `json.loads` can also raise
`ValueError` for oversized integers and `RecursionError` for excessive nesting.

Required outcome:

- skip these malformed lines with path/line-only warnings;
- never include payload content in warnings;
- add oversized-integer and deep-nesting regressions.

## 3. Required QA gaps

### Q1 — Add a real provider-to-renderer integration contract

Current CLI tests use a fake provider, while provider tests stop at normalized
models. Add one compact end-to-end test that uses the checked-in synthetic
Codex rollout through the real registry/provider and exports both HTML and
Markdown.

Assert semantically:

- user, assistant, and reasoning summary visibility/order;
- Bash input and folded output;
- generic apply_patch and MCP fallback preservation;
- absence of `wait`/`write_stdin` transport cards and raw command envelopes;
- absence of encrypted/opaque sentinels;
- stable tool-use/result correlation.

Avoid a huge full-document snapshot; use focused strings/DOM fragments.

### Q2 — Establish a provider contract suite

Add parametrized tests for Claude, AGY, and Codex covering:

- unavailable data directories;
- deterministic discovery and available-provider filtering;
- exact lookup and traversal rejection where applicable;
- normalized parent chaining;
- malformed input behavior;
- a strict **normalized-entry** `max_messages` cap.

Known inconsistencies to fix or explicitly document:

- `ClaudeProvider.load_session` ignores `max_messages`;
- AGY can exceed the cap when one raw record expands to multiple entries;
- registry constructor failures are swallowed without diagnostics.

If AGY/provider-abstraction cleanup is intentionally outside this PR, split it
from the arc rather than shipping untested baseline code.

### Q3 — Test ambiguity and conservative fallback paths

Add focused coverage for:

- duplicate session IDs in registry/CLI/provider lookup;
- Codex missing data dir, duplicate identity, OSError, non-object records,
  malformed envelopes, alternate message content, developer/system roles,
  unknown response types, and JSON fallback inputs/outputs;
- web-open mixed actions, malformed refs, missing output, separator mismatch,
  delimiters inside page content, and structured output;
- every provider-mode CLI conflict plus one render-options propagation spy;
- `render_normalized_session_file` format dispatch, parent directory creation,
  title behavior, and invalid renderer result.

### Q4 — Maintain a sanitized schema-variant corpus

Keep fixtures synthetic and privacy-scanned, but expand beyond the current
small happy-path rollout. Add variants derived from observed record families
and enumerate the documented families under `dev-docs/messages/codex` so a
documented shape cannot silently lose coverage.

## 4. Architecture and performance follow-ups

These should follow the correctness fixes, not obscure them in one large
rewrite.

### A1 — Split CodexProvider by responsibility

The provider now owns cataloging, identity/lineage, decoding, normalization,
async reconstruction, web batch expansion, and transport parsing in more than
1,000 lines. Introduce internal modules/types in staged commits:

1. rollout catalog/repository with cached identities and lazy records;
2. tolerant decoder with typed raw call/result helpers;
3. reconstruction passes for paired tools, async sessions, and batches;
4. transcript normalizer;
5. thin `CodexProvider` orchestration facade.

Use a typed paired-call/result object. `_adapted_call` must return
`AdaptedToolCall`, not `Any`.

### A2 — Eliminate repeated full-rollout scans

Discovery currently rereads identities, children, and shared parents; the CLI
then discovers all sessions before load rebuilds the index. Cache identity and
decoded records per path for one catalog operation, and use a linear/constrained
prefix algorithm. Add a synthetic many-child performance test or operation
counter so the fix remains measurable without timing flakes.

### A3 — Simplify registry/discovery ownership

Choose one public discovery facade. The current registry holds “lazy” classes
but eagerly instantiates them, ignores the supplied registration name during
instantiation, swallows all constructor errors, and is mirrored by mostly
unused functions in `discovery.py`.

Preferred direction:

- explicit provider factories/instances with name validation;
- logged initialization failures without leaking secrets;
- CLI routed through the same facade as other consumers;
- remove unused hooks (`get_session_format`, `get_session_stats`) or exercise
  them as real API contracts.

### A4 — Centralize final entry construction

Introduce an entry context/builder carrying session, model, cwd, version,
timestamp, and parent. Widen the shared tool-result helper to accept structured
content and error metadata. Avoid creating placeholder entries and mutating
their context afterward.

## 5. Secondary correctness items

- Preserve exit status/duration for user shell commands, or avoid specialized
  rendering for non-zero exits until the model supports the metadata.
- Fix the two ruff E402 errors in `providers/agy.py` as part of the provider
  contract cleanup.
- Make duplicate-ID behavior explicit and consistent instead of allowing the
  CLI dictionary comprehension to silently overwrite a discovered session.
- Cache discovery state only within one operation unless invalidation is
  designed; do not create stale long-lived registry state accidentally.

## 6. Implementation order and commit boundaries

Keep commits narrow and independently testable. Recommended order:

1. **`test: pin Codex adversarial correlations`**
   - add failing regressions for R1–R5 and R7;
   - include loaded child-content assertions and async negative cases.
2. **`fix: make Codex normalization conservative`**
   - fix local message dedup, async terminal folding, prefix stripping, and
     malformed JSON handling without architectural churn.
3. **`fix: harden Codex exec wrapper parsing`**
   - unify lexical scanning and privacy scrubbing; add property/table cases.
4. **`test: add cross-provider renderer regressions`**
   - freeze Claude/shared behavior affected by Task, WebSearch, and TodoWrite.
5. **`refactor: keep Codex result transport provider-local`**
   - move Codex-specific result adapters out of shared factories.
6. **`test: add provider contract and end-to-end exports`**
   - Q1/Q2/Q3, including real synthetic HTML/Markdown export.
7. **`fix: align provider contracts and ambiguity handling`**
   - strict max limits, registry diagnostics, duplicate IDs, AGY lint.
8. **`refactor: split Codex rollout pipeline`**
   - A1/A2 in small commits, retaining all regression tests.
9. **`refactor: simplify provider discovery and entry construction`**
   - A3/A4 only after the public behavior is pinned.
10. **`docs: update Codex provider support contract`**
    - document active/archived scope, known fallback behavior, hierarchy
      evidence, supported specialized tools, and QA corpus.

The first seven commits are expected before opening the PR. Architecture items
8–9 may be split into a follow-up only if profiling and review show their risk
outweighs their immediate value; record that decision explicitly rather than
silently dropping them.

## 7. Acceptance gates

Before the branch is ready for GitHub review:

- all new adversarial regressions pass;
- full pytest passes using the repository's supported serial/parallel split;
- full pyright passes;
- ruff passes for all files added or changed in the provider arc;
- fixture privacy scan passes and contains no real paths, remotes, credentials,
  opaque payloads, or account metadata;
- synthetic Codex session exports to HTML and Markdown through the real CLI;
- one real local session smoke-render completes without warning or content
  disclosure (do not commit the artifact);
- `git diff --check` passes;
- worktree contains no generated HTML or coverage artifacts in the commit;
- each release-blocking finding R1–R7 is linked to at least one regression;
- any deferred architecture item has an explicit follow-up issue/plan entry.

## 8. Delegation contract

The implementing actor owns the QA round on the existing branch. It should:

- treat this document as the prioritized backlog;
- begin with failing regressions, then fix behavior;
- preserve unrelated user/other-actor changes;
- commit only cohesive slices using the boundaries above;
- report after each slice with tests run and remaining findings;
- ask before removing worktrees, generated user artifacts, or materially
  changing the public provider API;
- stop and escalate if a proposed cleanup requires rewriting shared renderer
  semantics without pinned cross-provider tests.

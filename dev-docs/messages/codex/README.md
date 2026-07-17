# Codex rollout message samples

These examples document the Codex provider's persisted JSONL input. The raw
rollout format is an observed implementation detail, not a public compatibility
contract. Public Codex app-server item semantics remain the conceptual
reference for normalization.

## Provenance and safety

- Every value here and in `test/test_data/codex/` is hand-authored synthetic
  data. No line was copied from a real session.
- The modeled producer is Codex CLI `0.0.0-test`; real shapes were researched
  with `codex-cli 0.144.1` in July 2026.
- IDs, timestamps, models, paths, prompts, and tool outputs are deterministic
  placeholders. The only path used is `/workspace/synthetic-project`.
- Never add real prompts, outputs, system/developer instructions, encrypted
  reasoning, local paths, Git remotes, connector/account data, credentials, or
  rate-limit data.
- The encrypted-reasoning sentinel in the canonical test fixture exists solely
  to prove that the provider drops that field. It must never reach normalized
  entries, rendered output, or warnings.

The canonical end-to-end fixtures live in `test/test_data/codex/`. Files below
are deliberately small illustrations rather than a second test contract. The
machine-checked [`manifest.json`](manifest.json) enumerates every documented
family so additions cannot silently lose corpus coverage.

## Record families

- [`session/session_meta.json`](session/session_meta.json) establishes thread
  identity and basic metadata. The first metadata record owns identity.
- [`messages/visible-pair.json`](messages/visible-pair.json) shows duplicate
  visible-message representations; normalization emits the message once.
- [`messages/environment-context.json`](messages/environment-context.json) and
  [`messages/user-shell-command.json`](messages/user-shell-command.json) show
  structured user-side context and local shell records.
- [`reasoning/summary.json`](reasoning/summary.json) shows readable summaries.
  Encrypted content is never rendered.
- [`tools/function-call.json`](tools/function-call.json) shows `call_id`
  correlation. Tool names are open-ended; structured and custom calls differ.
- [`tools/exec-wrapper.json`](tools/exec-wrapper.json),
  [`tools/async-command.json`](tools/async-command.json), and
  [`tools/web-run.json`](tools/web-run.json) cover conservative wrapper
  specialization, terminal polling, and web transport.
- [`lifecycle/turn-context.json`](lifecycle/turn-context.json) carries mutable
  turn metadata rather than visible conversation content.
- [`collaboration/thread-spawn.json`](collaboration/thread-spawn.json) records
  parent/child identity used by later hierarchical rendering.
- [`collaboration/agent-tools.json`](collaboration/agent-tools.json) shows the
  direct spawn/result correlation used by Task adaptation.
- [`legacy/flat-rollout.md`](legacy/flat-rollout.md) describes the supported
  flat discovery layout.

Unknown records and malformed lines are non-fatal. Diagnostics may identify a
file and line number, but must not include raw record content. Known gaps include
the exact compaction representation, interrupted/delta recovery, image shapes,
and broader compatibility across Codex versions and surfaces.

## Provider support contract

- Discovery reads active rollouts under `$CODEX_HOME/sessions`; archived
  sessions are outside the initial support boundary.
- Visible event/response copies are deduplicated only when adjacent. Repeated
  text in separate turns remains visible.
- Parent history is stripped only with a multi-record parent-suffix match or a
  unique stable spawn-call boundary.
- Async command polling folds into Bash only across invisible metadata and only
  after a terminal exit result. Live or ambiguous handles remain lossless.
- Static `exec_command`, plan, collaboration, agent-list, search-only web, and
  exact open-only web batches reuse existing typed renderers. Dynamic,
  compound, malformed, mixed-action, and unknown calls keep generic Workflow
  or raw tool rendering.
- Opaque agent payloads are scrubbed before every Workflow fallback. Encrypted
  reasoning is never inspected or rendered.
- Successful user-shell commands use the compact Bash presentation. Non-zero
  commands retain their original envelope so exit status and duration remain
  visible.

The staged internal refactors intentionally deferred from this correctness
round are recorded in
[`work/codex-architecture-followups.md`](../../../work/codex-architecture-followups.md).

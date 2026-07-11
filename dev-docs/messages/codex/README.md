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
are deliberately small illustrations rather than a second test contract.

## Record families

- [`session/session_meta.json`](session/session_meta.json) establishes thread
  identity and basic metadata. The first metadata record owns identity.
- [`messages/visible-pair.json`](messages/visible-pair.json) shows duplicate
  visible-message representations; normalization emits the message once.
- [`reasoning/summary.json`](reasoning/summary.json) shows readable summaries.
  Encrypted content is never rendered.
- [`tools/function-call.json`](tools/function-call.json) shows `call_id`
  correlation. Tool names are open-ended; structured and custom calls differ.
- [`lifecycle/turn-context.json`](lifecycle/turn-context.json) carries mutable
  turn metadata rather than visible conversation content.
- [`collaboration/thread-spawn.json`](collaboration/thread-spawn.json) records
  parent/child identity used by later hierarchical rendering.
- [`legacy/flat-rollout.md`](legacy/flat-rollout.md) describes the supported
  flat discovery layout.

Unknown records and malformed lines are non-fatal. Diagnostics may identify a
file and line number, but must not include raw record content. Known gaps include
the exact compaction representation, interrupted/delta recovery, image shapes,
and broader compatibility across Codex versions and surfaces.

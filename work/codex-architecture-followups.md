# Codex provider architecture follow-ups

> Recorded: 2026-07-14
> Branch: `dev/codex-tools`

The QA hardening round deliberately leaves A1–A4 out of the release branch.
The correctness fixes now have adversarial, cross-provider, and end-to-end
coverage; splitting the pipeline in the same round would increase review risk
without changing the support contract.

## A1 — Split provider responsibilities

Extract, in order, a rollout catalog, tolerant decoder, reconstruction passes,
and transcript normalizer. Keep `CodexProvider` as the orchestration facade.
Introduce a typed paired-call/result value and make `_adapted_call` return a
typed adapter rather than `Any`. Each extraction must preserve the current
adversarial and provider-to-renderer suites unchanged.

## A2 — Remove repeated rollout scans

Cache identities and decoded records for one discovery/load operation only.
Before changing the algorithm, add an operation-count test with many synthetic
children; assert linear or constrained scans without relying on wall-clock
timings. Do not introduce long-lived cache state until invalidation is defined.

## A3 — Simplify registry/discovery ownership

Choose one discovery facade, replace class-name registration with validated
factories or instances, and remove or exercise unused provider hooks. The
hardening round already adds deterministic ordering, name validation, and
sanitized initialization diagnostics; preserve those contracts.

## A4 — Centralize entry construction

Add an entry context/builder for session, model, cwd, version, timestamp, and
parent chaining. Widen the result boundary only with cross-provider tests for
structured content and error metadata. Avoid placeholder entries that are
mutated after construction.

## Exit criteria

- No provider or renderer support-contract change.
- Full provider contract, Codex adversarial, and HTML/Markdown export suites
  remain green after every extraction.
- A2 includes an operation-count assertion demonstrating the improvement.
- Public API changes, if any, are reviewed separately from internal moves.

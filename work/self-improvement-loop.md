# Self-Improvement Loop — Design Draft

Status: **exploratory, no code.** Arose from evaluating
[Docent](https://github.com/TransluceAI/docent) as an analysis layer over
CCL's transcripts (2026-09-19). The conclusion was that Docent's *interaction
pattern* is worth keeping and its implementation isn't; this sketches what to
build on CCL's parse instead. Every number below is either measured from
Docent's source or quoted from a vendor page — both are marked. Open
decisions at the bottom.

## Motivation

The goal is **per-project agent improvement over time**, not a dashboard for
a human to admire. Concretely, questions like:

- What in this session should have been a skill? (multi-turn trial and error
  that recurs)
- Which CLAUDE.md instruction should be retired?
- Which docs are stale or actively misleading?
- How much, and how successfully, are the configured skills and MCP servers
  actually used?
- Which lint rule is consistently ignored? Is the test suite too verbose?

The consumer of the output is the **agent** (via CLAUDE.md, skills, repo
docs), with a human approving each change.

### The asymmetry that shapes everything

A report a human reads and ignores costs a minute. **An instruction the agent
obeys degrades every subsequent session in that project** — silently, and
compounding. And the loop has *no counterfactual*: propose a rule, the next
session obeys it, the transcripts confirm it was followed, and nothing ever
flags it as wrong. You cannot observe the session you would have had without
it.

So the failure mode to design against is not "bad suggestions". It is
**monotonic growth** — rules only ever get added, never removed, until
CLAUDE.md is 400 lines of accumulated superstition. Retirement has to be a
first-class operation with its own budget, or additions crowd it out forever
(additions always have fresh evidence; deletions never do).

## Why not Docent

Docent is an AI-agent transcript analysis platform (Apache-2.0,
self-hostable: Postgres + pgvector, Redis, FastAPI, arq worker, Next.js).
Findings from reading the source at `8ab199c`:

| Finding | Where |
|---|---|
| The judge gets `ar.to_text_new()` — the **whole run**, no token limit. `to_str_with_token_limit` exists in the SDK and is called nowhere in `docent_core`. | `ai_tools/rubric/rubric.py:235` |
| On context overflow the call raises `ContextWindowException`, is caught per item, and yields **no judge result** — the job reports complete with the oversized runs silently missing. | `_llm_util/prod_llms.py:168` |
| Clustering = one LLM call to propose label strings from a shuffled, 100k-token-truncated list, then **one call per (result × label)** binary membership test. Default assigner `o4-mini`. | `ai_tools/clustering/*` |
| Semantic search is dead code: `_rerank_agent_runs_by_embeddings` and `execute_search` have no callers, yet the embedding job still runs on every ingest. | `services/monoservice.py:955` |
| `multi_round_clustering.py` and `modal_assigner.py` — no callers. | — |
| Ingestion is fine and simple: `POST /rest/{cid}/agent_runs` with plain Pydantic JSON; the SDK is optional. | `sdk/client.py:107` |

Worth keeping: the **question → explore → refine into a rubric → measure**
loop, and the propose-then-verify clustering shape (independent membership
tests with a stored reason beat one-shot bucketing). Worth discarding: whole
transcripts into a frontier judge, and the Postgres.

The decisive mismatch is scale. Docent is built for eval logs, which are
small. A Claude Code session that has compacted has by definition already
filled a context window, and CCL's export carries *both* sides of the
compaction.

## Architecture: three grading tiers

The principle is that the frontier model's job is **compiling a question into
a grader**, once, rather than grading every session forever.

### Tier 0 — deterministic, over CCL's parse

Tool-call sequences, exit codes, `is_error` results, read-then-edit pairs,
adherence predicates, recurrence counts. Free, exact, **backfillable over the
whole archive in seconds**, and regression-testable: a new grader can be run
over last month and produce a trend line, which no LLM pipeline gives you.

Tier 0 does not only filter — it **locates**. It extracts *spans*: "the six
messages around each error result", "each Edit to a file read in the same
turn". This is the substrate the other tiers consume, and it is the part that
genuinely needs CCL (the DAG, sidechain splicing, tool_use/tool_result
pairing).

### Tier 1 — Jev over spans

[TypeSafe Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
(launched 2026-09-15) returns typed, calibrated probabilities instead of
text. Vendor-stated figures:

- `Choice` (1 of ≤255), `Score` (2–10 level spectrum), `Noul` (probability
  0–1). Multiple questions **evaluated in parallel** per request.
- **$0.042/MTok input, output free.** 70–500ms end-to-end.
- **64k tokens total; 32k for state + longest question.**
- Documented design principle: *"Retrieve and filter in code first, and send
  only the fields the question needs"* — accuracy degrades with irrelevant
  state, deliberately.

Three properties matter here. The context cap **enforces** the fragment
discipline rather than leaving it to willpower. Parallel questions make a
*checklist* the native unit of work. And the calibrated probability gives a
principled escalation threshold instead of a vibe.

Caveats: days old at time of writing, calibration is the vendor's claim and
must be checked against our own labelled set, and it is a hosted API (spans
leave the machine).

### Tier 2 — frontier model on the residual

Only spans whose Jev probability sits in the uncertain band, and only the
span — never the session.

### The arithmetic

One rubric over ~800 sessions:

| Approach | Tokens | Cost |
|---|---|---|
| Whole session → Sonnet 5 judge (Docent's shape) | 800 × ~200k = 160M | ~$320 |
| 20 spans × 500 tokens → Jev | 8M | ~$0.34 |

Note where the win comes from: **the fragment discipline buys ~400× and the
model choice ~50×**. The expensive, valuable engineering is tier 0. Jev makes
the residual nearly free; it does not make the architecture.

## Lanes and the scheduler

### The unit is a question, not a finding

A *lane* is a standing question ("is the test suite too verbose?") with
state: trigger predicate, last examined, last verdict, evidence count at last
look, threshold, dormancy level.

### Gate on evidence delta, not elapsed time

A lane is re-eligible only when its tier-0 evidence has changed materially
since the last look — matching span count grown past a threshold, or a new
*kind* of matching span. If the evidence is unchanged, the answer cannot have
changed, so spending a token is waste.

A cooldown timer is a proxy for this; evidence delta is the actual condition.
The steady state is therefore **silence**: most cycles run counters and
produce nothing. That is the answer to "millions of tokens on bookkeeping" —
the bookkeeping is not LLM work, it is a counter table.

### Dormancy: spend proportional to human engagement

Rather than expiring unreviewed proposals, a lane whose proposals go
unreviewed **escalates through dormancy levels**:

1. **Active** — full pipeline, tier 0 → Jev → frontier proposal.
2. **Throttled** — tier 0 + Jev only; evidence accrues, no proposal written.
3. **Dormant** — tier 0 only. No LLM invocation at all for this lane.

Enough dormant lanes and the `claude -p` invocation is skipped entirely; the
loop degrades to cheap deterministic grading and keeps accruing evidence. A
human looking at the queue resets the levels.

This is better than expiry: **the system's LLM spend becomes proportional to
the human's engagement with it**, automatically, with no tuning. Neglect
throttles it instead of piling up debt, and nothing is lost — the evidence is
still being counted, so a return to the queue finds a stronger case than when
it was abandoned.

### Rejections set state, they are not prose

If notes are only replayed into the next prompt, the same arguments get
re-litigated forever and paid for each time. Instead the note sets lane
state:

| Rejection | Effect |
|---|---|
| **Not now** | Raise this lane's evidence threshold — re-ask at ~3× the evidence |
| **Wrong — that's not what happened** | Grader is miscalibrated. The span becomes a **labelled example** in the eval set; lane survives, grader needs fixing |
| **Never** | Tombstone; do not ask again |

This separates *"real but not worth acting on"* from *"false"*, which
free-text blurs. The second grows the eval set for free as a by-product of
triage — which is the only way we ever learn whether the graders are any
good.

### A reserved retirement slot

Per cycle, cap proposals (default 3) and reserve one slot for a
**retirement** candidate — or nothing. Without this, monotonic growth returns
through the scheduler.

## The ledger

### It lives in its own SQLite DB, not the cache

The cache (`claude-code-log-cache.db`) is **derived and disposable** — it
rebuilds on schema-version bump or source change. The FTS search index lives
inside it precisely because it is also derived (`search.py`, keyed on
`cached_files.cached_mtime`). The ledger is the opposite: hand-annotated
rejection notes are the most expensive data in the system and must survive a
cache wipe. Separate file alongside the cache; ideally version-controlled.

### Queried, not read

"Analyse past interventions" is the part that grows without bound. Tier 0
answers *"what did we do about X, when, and did the metric move?"* as rows;
only the rows for this cycle's lanes reach the model. The archive is never
replayed into a prompt.

### It generates the highest-value questions

If each accepted intervention records **which tier-0 metric it was meant to
move**, the best question in the system writes itself, deterministically and
free:

> Intervention #17 (CLAUDE.md rule about `--snapshot-update -n0`) was meant to
> reduce snapshot-corruption incidents. Twenty sessions on, has it?

That is the only thing in the design that closes the loop, and it supplies
retirement criteria with evidence attached: an intervention that moved no
metric is a deletion candidate.

## Output: proposals, then PRs

**v0:** proposals land in the queue as structured rows with a rendered
summary. Accept/reject/note from a CLI (or the TUI).

**Batching.** A one-line PR to review every day is worse than nothing. So the
PR step is a subagent of the `claude -p` cycle, gated on **≥ n actionable
findings (default 5)** — and plausibly the model's own call: *"is there
enough material here for a PR that would move the needle?"* A cheap
deterministic floor plus a judgement call above it.

**Later, split by reversibility rather than category:**

- Mechanical and easily reverted (retire a lint rule, trim test verbosity,
  refresh a stale doc) → **a branch with a diff**. A commit you glance at and
  merge or bin beats a paragraph asking permission, and it gives the
  accept/reject signal a natural home (merged / closed / closed-with-comment).
- Changes to the agent's own instructions → **a proposal**, because that is
  where being wrong is invisible and compounding.

## Data model sketch

Second SQLite DB alongside the cache. Roughly:

- `lanes` — id, question, trigger predicate ref, grader ref, threshold,
  dormancy level, last_examined, evidence_at_last_look, verdict.
- `spans` — lane_id, session_id, message uuid range, tier-0 match reason,
  jev_probability, escalated (bool). The evidence.
- `proposals` — lane_id, kind (create_skill | retire_instruction |
  update_doc | tooling | other), body, evidence span ids, created_at,
  state (pending | accepted | rejected | expired), note, decided_at.
- `interventions` — proposal_id, what changed, where, target_metric,
  baseline_value, applied_at. The ledger proper.
- `labels` — span_id, human verdict, source (rejection note | hand-labelled).
  The eval set.
- `metrics` — session_id, per-session tier-0 counters (turns, rework, error
  streaks, permission prompts, compactions, skill/MCP invocations + error
  rates). Cheap to recompute, but stored so trend queries don't re-parse.

`metrics` and `spans` are arguably derived and could be rebuilt; `labels`,
`proposals` and `interventions` are not, and that split should be explicit
so a rebuild can never eat them.

## Where this lives

**Almost certainly a separate library that depends on `claude-code-log`,**
not a CCL subpackage:

- Different remit. CCL converts and renders transcripts; this mutates a repo
  based on inferences about them.
- Different dependencies (Jev client, `claude -p` orchestration, a mutable
  ledger) and a different release cadence.
- CCL's value here is entirely its **parse layer**, which is stable and
  general; the loop's logic is opinionated and personal.

**The coupling point is the risk.** If the add-on reaches into CCL internals,
every CCL refactor breaks it. The CCL-side ask is therefore small and
concrete: **a documented, stable extraction API** — today's entry points
(`converter.load_transcript`, `converter.load_directory_transcripts`,
`cache.get_cache_db_path`) are shaped for rendering, not for span extraction.
Defining that surface is the one piece of work that belongs in this repo.

Open: whether span extraction primitives ("messages around each error
result", "read-then-edit pairs") are general enough to live in CCL, or are
loop-specific and belong in the add-on. Leaning towards the add-on until a
second consumer appears.

## v0 scope

Deliberately smaller than the above:

1. Tier-0 counters over the existing cache, written to the new DB. No LLM, no
   Jev, no proposals.
2. A hand-written ledger and a weekly look at the table.
3. Hand-label ~50 spans to seed the eval set.

**If a month of that never surfaces one intervention worth making, the whole
pipeline is moot** — and that is an afternoon's work to find out, rather than
a fortnight's. Only after that: graders, then Jev, then proposals, then PRs.

## Open questions

- **What does "improve" mean, in a number?** Candidate tier-0 outcome
  proxies: turns to completion, rework rate (files edited then re-edited),
  error-streak length, permission prompts, compaction count. If proposals
  move none of them, they are opinions with infrastructure. But the
  improvement that matters may only be recognisable by feel — in which case
  the honest design is a proposal queue with measurement as a backstop, not
  a closed loop.
- **Per-project or global queues?** Skills and doc rot are project-scoped;
  verbosity preferences and tool-usage patterns are user-scoped. Probably
  both, at different cadences.
- **Who runs the cycle?** A `SessionEnd`/`Stop` hook (transcript path is to
  hand, but fires constantly), a cron, or an explicit CLI invocation. Leaning
  towards: hook does tier 0 only; cycle is cron or manual.
- **Does the calibration hold on our data?** Jev's thresholds are only
  trustworthy once checked against the labelled set. Until then, treat the
  probability as a ranking, not a decision.
- **Grader authoring UX.** A rubric that compiles to a predicate + a Jev
  checklist + a prompt is a much better artefact than a prompt — versionable,
  diffable, testable. But somebody has to review the generated predicate.
  How much review is enough?

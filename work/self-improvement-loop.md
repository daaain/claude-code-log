# Self-Improvement Loop — Design Draft

Status: **exploratory, no code yet.** It came out of evaluating
[Docent](https://github.com/TransluceAI/docent) as a way to analyse CCL's
transcripts (2026-09-19). We concluded that Docent's *way of working* is
worth copying but its implementation isn't. This doc sketches what to build
on top of CCL's parser instead.

Revised 2026-09-27 after a review pass: plainer language, plus the decisions
recorded in [Decisions so far](#decisions-so-far). Every number is marked as
either measured from Docent's source or quoted from a vendor's page.

## Words used in this doc

| Term | Meaning |
|---|---|
| **Fact table** | A flat table pulled out of the transcripts by code, e.g. one row per tool call with its name, whether it errored, and the error text. Cheap to rebuild. |
| **Query** | Plain SQL over the fact tables, e.g. "tool errors per week, grouped by likely cause". |
| **Excerpt** | A few consecutive messages picked out by code as evidence, e.g. the six messages around a failed tool call. What a model is given to start from. |
| **Signature** | An error's text with the variable parts (paths, line numbers, IDs, timestamps) stripped out, so the same failure in different places groups together. |
| **Standing question** | A question we keep asking over time ("is the test output too verbose?"), plus what we know about it: when we last looked, what we concluded, and how much evidence there was then. |
| **Proposal** | A suggested change (a new skill, a retired instruction, a doc fix) waiting for a human to accept or reject it. |
| **Ledger** | The record of proposals, decisions, and changes actually made. |

## Motivation

The goal is **to make the agent work better in each project over time**. It
is not a dashboard for a human to admire. Typical questions:

- What in this session should have been a skill? (Trial and error across
  several turns that keeps happening.)
- Which CLAUDE.md instruction should be retired?
- Which docs are stale or actively misleading?
- How much, and how successfully, are the configured skills and MCP servers
  actually used?
- Which lint rule is consistently ignored? Is the test suite's output too
  verbose?

The **agent** consumes the output, through CLAUDE.md, skills, and repo docs.
A human approves each change.

### Why bad advice is worse here than usual

A report a human reads and ignores costs a minute. **An instruction the
agent obeys makes every later session in that project worse**, silently,
and the damage adds up. And there is **no way to see what would have
happened otherwise**. We propose a rule, the next session obeys it, the
transcripts confirm it was followed, and nothing ever flags it as wrong.

So the main danger isn't bad suggestions. It's that **rules pile up**. Rules
get added and never removed, until CLAUDE.md is 400 lines of accumulated
superstition. Removing rules has to be a first-class operation with its own
reserved share of attention. Otherwise additions always win, because they
always have fresh evidence and removals never do.

## Why not Docent

Docent is a platform for analysing AI agent transcripts (Apache-2.0,
self-hostable: Postgres + pgvector, Redis, FastAPI, arq worker, Next.js).
Findings from reading its source at `8ab199c`:

| Finding | Where |
|---|---|
| The judging model is given the **whole run** (`ar.to_text_new()`) with no token limit. A token-limited variant exists in the SDK but nothing calls it. | `ai_tools/rubric/rubric.py:235` |
| If a run doesn't fit in the context window, the error is caught per item and the run simply gets **no result**. The job still reports success with those runs missing. | `_llm_util/prod_llms.py:168` |
| Grouping results works in two steps. One model call invents group labels from a shuffled list cut off at 100k tokens. Then **one call per (result, label) pair** asks "does this belong?". Default model is `o4-mini`. | `ai_tools/clustering/*` |
| Semantic search is dead code: its functions have no callers, yet embeddings are still computed on every import. | `services/monoservice.py:955` |
| `multi_round_clustering.py` and `modal_assigner.py` have no callers. | — |
| Importing data is simple and fine: plain JSON to one REST endpoint. | `sdk/client.py:107` |

Worth keeping:

- The workflow **ask a question → explore → turn it into a checklist →
  measure**.
- Grouping done as "propose labels, then check each item against each label
  separately, keeping the reason". It beats sorting everything in one go.

Worth discarding: feeding whole transcripts to an expensive model, and the
Postgres.

The deciding problem is size. Docent is built for evaluation logs, which are
short. A Claude Code session that was compacted has, by definition, already
filled a context window, and CCL's output includes *both* sides of the
compaction.

## Architecture: three stages

The expensive model should be used **once, to turn a question into
something code can check**, not to read every session forever.

### Stage 1: extract with code (no model)

Parse the transcripts with CCL and write a few **fact tables**: tool calls
(name, input summary, errored?, error text), sessions (model, effort, Claude
Code version, duration, compactions), file reads and edits. Everything else
is a **SQL query** over those tables. We don't maintain precomputed
counters; see [decision 9](#decisions-so-far).

This stage is free, exact, and can be **re-run over the whole archive in
seconds**. So a new query can be tried on last month's data and produce a
trend line straight away, which no model-based pipeline gives you.

Stage 1 also **picks out excerpts**, such as "the six messages around each
failed tool call" or "each edit to a file read earlier in the same turn".
The later stages start from these excerpts. This is the part that really
needs CCL: it understands session forks, splices subagent transcripts in,
and pairs each tool call with its result.

And it **clusters**, still without a model. For errors, the starting point
is grouping by tool plus error **signature**, so "`uv: command not found`
in 40 sessions" becomes one cluster with 40 members instead of 40 rows to
read. The later stages then work per cluster, looking at a few
representative excerpts, not per error.

The error text alone is weak evidence, though: `No such file or directory`
can mean a stale doc or a guessed path. So clustering also uses the failed
call's **neighbours in the message graph**, which is still cheap string
work:

- **Where the failing argument came from.** Search the call's ancestors for
  the path or command it used. If it appears verbatim in an earlier read of
  a doc, skill, CLAUDE.md or memory file, that's a *stale instructions*
  candidate. If it appears nowhere, the agent guessed it. If it came from
  an earlier tool's output, the environment misled it.
- **What fixed it.** Find the next successful call to the same tool and
  compare the inputs (`pytest` → `uv run pytest`). That difference is often
  the most useful thing in the whole cluster, and a strong clustering key.
- **What happened next** if nothing fixed it: retried, went to read docs,
  asked the user, or gave up.

The "where it came from" check only works once injected context
(CLAUDE.md, memory) is attached to the session; see the prerequisite
section near the end.

A fix that **recurs** across many sessions is the natural first source of
proposals: forty sessions of `pytest` → `uv run pytest` is practically a
proposal already. Detection only says "this keeps happening, and this is
what fixes it". Whether the answer is an instruction, a skill, or changing
the environment so no instruction is needed is decided downstream.

### Stage 2: cheap yes/no checks on excerpts (Jev)

[TypeSafe Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
(launched 2026-09-15) returns typed answers with a confidence, not free
text. Vendor-stated figures:

- Answer types: pick 1 of up to 255 options, a score on a 2–10 point scale,
  or a probability from 0 to 1. **Several questions can be asked in one
  request.**
- **$0.042 per million input tokens; output is free.** 70–500ms per request.
- **64k tokens in total; 32k for the input plus the longest question.**
- Their own advice: *"Retrieve and filter in code first, and send only the
  fields the question needs"*. Accuracy deliberately drops when you include
  irrelevant material.

Three things make it a good fit:

- The small context limit **forces** us to send excerpts rather than whole
  sessions, so we can't cheat.
- Asking several questions at once makes a **checklist** the natural unit
  of work.
- The confidence gives a principled threshold for "send this to a bigger
  model" instead of guesswork.

Caveats:

- It was only a few days old when this was written.
- The confidence figures are the vendor's claim. We have to check them
  against examples we have labelled by hand.
- It is a hosted API, so excerpts leave the machine. That's acceptable for
  this use; see [decision 7](#decisions-so-far).

### Stage 3: a frontier model investigates the uncertain cases

Only clusters where Jev's confidence is in the uncertain middle reach a
frontier model. It **starts from the excerpt, but may go back to the full
transcript** to investigate: read further back, check what the agent had
been told, look at the repo's git history at that point. The rule is
*pull, don't push*. The model is never handed a whole session up front; it
fetches only what it decides it needs, through tools.

Every investigation is **recorded as evidence**: what it looked at (session
and message ids, files, commits), what it concluded, and why. A human
reviewer or a later agent can then retrace it without redoing it. These
records cost real money to produce, so they belong with the irreplaceable
data (see the data model).

Because an investigation's size isn't fixed by the excerpt any more, each
one needs a **budget** (tokens or tool calls), a setting like any other.
CCL's archive search (`search.py`, served by `api.py`) is a natural
starting point for the investigator's tools.

### Rough costs

One question asked across ~800 sessions:

| Approach | Tokens | Cost |
|---|---|---|
| Whole session → Sonnet 5 as judge (Docent's approach) | 800 × ~200k = 160M | ~$320 |
| 20 excerpts × 500 tokens → Jev | 8M | ~$0.34 |

Most of the saving comes from **sending excerpts instead of sessions
(~400×)**. Switching to a cheaper model gives only ~50×. So the valuable
engineering is stage 1. Jev makes the leftover cases nearly free, but it
isn't what makes the design work.

## Standing questions and scheduling

### The unit of work is a question, not a finding

A standing question stores:

- what makes it relevant (a SQL query that finds evidence);
- how it is judged (a checklist);
- when we last looked, and what we concluded;
- how much evidence there was at the time;
- how much new evidence is needed before we look again;
- how far it has backed off (see below).

### Only look again when there's new evidence

A question is only worth asking again when the evidence has changed
noticeably. That means more matching excerpts than a threshold, or a new
*kind* of match. If the evidence hasn't changed, the answer can't have
either, so spending tokens on it is waste.

A timer ("look again after a week") only approximates this; the real
condition is new evidence. So on most runs **nothing happens**: the SQL
runs and produces nothing new. Keeping track is a table lookup, not model
work, so it costs next to nothing.

### Back off when nobody is reviewing

Unreviewed proposals don't expire. Instead, a question whose proposals go
unreviewed **steps down through three levels**:

1. **Active**: the full pipeline, from extraction through the Jev checks to
   a frontier-model proposal.
2. **Throttled**: extraction and Jev checks only. Evidence keeps building,
   but no proposal is written.
3. **Paused**: extraction only. No model calls for this question.

If enough questions are paused, the `claude -p` call is skipped altogether.
The loop falls back to cheap code-only checks while evidence keeps
building. Looking at the queue resets the levels.

This beats expiry because **model spending then tracks how much attention
the human is paying**, automatically and without tuning. Neglect slows the
loop down instead of piling up a backlog, and nothing is lost. The evidence
keeps accumulating, so coming back to the queue finds a stronger case than
the one left behind.

### Rejections change what happens next

If rejection notes are only fed back into the next prompt, the same
arguments get made again forever, and we pay for each round. Instead each
kind of rejection changes the question's settings:

| Rejection | Effect |
|---|---|
| **Not now** | Raise the evidence threshold, so we ask again at ~3× the evidence. |
| **Wrong: that's not what happened** | The check itself is wrong. The excerpt becomes a **labelled example** in the test set. The question stays, but its check needs fixing. |
| **Never** | Close the question permanently. |

This separates *"true, but not worth acting on"* from *"false"*, which a
free-text note blurs. The second kind grows our set of hand-labelled
examples as a side effect of triage. Those examples are the only way we'll
ever know whether the checks are any good.

### A slot reserved for removals

Each run proposes at most a few changes (default 3), and one slot is
reserved for **retiring** something, or left empty. Without this, rules
piling up comes back through the scheduler.

All of these numbers (thresholds, the ~3×, the proposal cap, the PR
minimum below) are **settings with sensible defaults**, not constants.

## The ledger

### It has its own SQLite database, separate from the cache

The cache (`claude-code-log-cache.db`) is **derived and disposable**. It is
rebuilt whenever its schema version changes or source files change. The
search index lives inside it for exactly that reason: it's derived too
(`search.py`, keyed on `cached_files.cached_mtime`).

The ledger is the opposite. Hand-written rejection notes and labels are the
most expensive data in the system, so they must survive a cache wipe. They
are also application data and potentially sensitive, so they live in their
own SQLite file next to the cache. They are **not** kept in git; see
[decision 5](#decisions-so-far).

### Look things up; never replay the history

The record of past changes will keep growing. SQL answers *"what did we do
about X, when, and did the numbers move?"* as rows. Only the rows for this
run's questions reach the model. The history is never pasted into a prompt.

### It suggests the most valuable questions itself

Suppose each accepted change records **which query result it was meant to
move**. Then the most useful question in the system writes itself, for
free and without a model:

> Change #17 (the CLAUDE.md rule about `--snapshot-update -n0`) was meant
> to reduce snapshot-corruption incidents. Twenty sessions later, has it?

This is the only part of the design that closes the loop. It also gives
removal candidates with evidence attached: a change that moved nothing
should probably be retired.

**Caution: before/after comparisons are confounded.** A new model, a
different effort level, a new Claude Code version, or just a different mix
of tasks that month can move the numbers far more than a CLAUDE.md tweak.
So every comparison must be **grouped by model, effort, and Claude Code
version**, and should claim "the rule was followed and the problem it
targets happened less often in comparable sessions", not "the rule caused
the improvement".

## Output: proposals, then PRs

**v0:** proposals land in a queue as rows with a readable summary. Accept,
reject, or add a note from a CLI (or the TUI).

**Batching.** Reviewing a one-line PR every day is worse than nothing. So
PRs come from a subagent of the `claude -p` run, and only when there are
**at least n actionable findings (default 5)**. Above that minimum, the
model makes its own call: *"is there enough here for a PR that would make a
real difference?"*

**Later: split by how easy a change is to undo, not by topic.**

- **Mechanical changes that are easy to revert** (retire a lint rule, trim
  test output, refresh a stale doc): **a branch with a diff**. A commit you
  glance at and then merge or bin beats a paragraph asking permission. It
  also records the accept/reject decision naturally, as merged, closed, or
  closed with a comment.
- **Changes to the agent's own instructions**: **a proposal**, because
  that's where a mistake is invisible and keeps compounding.

## Data model sketch

Two groups of tables in the ledger database.

**Rebuildable from transcripts** (a rebuild may drop and recreate these):

- `tool_calls`: session_id, message uuid, timestamp, tool name, input
  summary, is_error, error text, error signature. One row per call; each
  error counts once (see [decision 11](#decisions-so-far)).
- `tool_calls` also gets the neighbour facts: where the failing argument
  came from (instructions | guess | tool output), the fixing call's uuid and
  input difference if any, and what happened next.
- `clusters`: signature, tool, argument source, fix pattern, member count,
  first/last seen, likely cause (filled in by stages 2–3).
- `sessions`: session_id, project, model, effort, Claude Code version,
  start/end, turns, compactions, git branch/commit if known.
- `excerpts`: question_id, session_id, message uuid range, why it matched,
  Jev probability, whether it was sent on to stage 3.

**Irreplaceable** (a rebuild must never touch these):

- `questions`: id, question text, evidence query, checklist, threshold,
  back-off level, last looked at, evidence count at that time, last
  conclusion.
- `proposals`: question_id, kind (create_skill | retire_instruction |
  update_doc | tooling | other), body, supporting excerpt ids, created_at,
  state (pending | accepted | rejected | expired), note, decided_at.
- `changes`: proposal_id, what changed, where, the query it should move,
  the value before, applied_at. This is the ledger proper.
- `investigations`: cluster or excerpt id, stage (Jev | frontier), what it
  looked at, conclusion, reasoning, cost, created_at. Expensive to
  reproduce, so kept.
- `snapshots`: content hash, original path, first seen, content. A copy of
  each mutable file (memory, CLAUDE.md outside git) the first time evidence
  points at a given version of it. Stored once per distinct content.

**How evidence points at things.** Transcripts are kept, so evidence stores
pointers, not copies, in three layers:

1. **Messages: `(session_id, message uuid)`**, resolved through CCL's
   parse. Never a cache row id, since the cache is rebuilt and row ids
   change. UUIDs also survive the occasional rewritten transcript.
2. **Repo files: `(repo, commit, path)`**, resolved through git.
3. **Files in `~/.claude` that nothing extracts yet** (memory and the
   like): a **content hash** into `snapshots`. A plain path isn't enough,
   because these files are edited in place (see *To investigate*): by the
   time someone follows the pointer, the file may say something else.
- `labels`: excerpt_id, human verdict, source (rejection note or labelled by
  hand). This is the test set.

The split should be enforced in code (separate schemas or a guard), so a
rebuild can never delete the irreplaceable group.

## Where it lives

**Long term: probably a separate library that depends on
`claude-code-log`**, not part of CCL itself:

- Its job is different. CCL converts and displays transcripts; this changes
  a repo based on guesses about them.
- Its dependencies (a Jev client, `claude -p` orchestration, git, a mutable
  ledger) and release rhythm are different.
- What it needs from CCL is the **parser**, which is stable and general. The
  loop's logic is opinionated and personal.

**For the prototype: an isolated directory in this repo** (see
[decision 4](#decisions-so-far)). It may reach into CCL internals freely,
but we **keep a list of every internal it touches**. When the prototype has
proven itself, that list is the spec for a stable CCL API. It's based on
real use, not guessed in advance.

## v0 scope

Deliberately small:

0. The inventory step of the harness-context prerequisite (see below):
   find out what's logged and what isn't. Attaching the missing pieces can
   follow; until then, "guessed" means "not found in anything we can see".
1. A separate CLI command that parses incrementally (only files changed
   since the last run) and fills the `tool_calls`, `sessions` and
   `clusters` tables. Run it by hand or from cron.
2. A handful of SQL queries, starting with **tool-call errors clustered by
   signature**, largest clusters first. The causes we expect to find:
   - environment or sandbox (missing command, permission denied, network
     blocked);
   - harness (tool or hook misbehaving);
   - stale docs or skills (the agent followed instructions that no longer
     match reality).

   In v0 a human reads the top clusters and assigns causes. Stages 2 and 3
   automate exactly this step later, so the hand-assigned causes double as
   their first test set.
3. A hand-kept ledger and a weekly look at the results.
4. Hand-label ~50 excerpts to start the test set.

**The v0 bet:** grouping errors by cause turns up at least one fix worth
making within a month. After the fix, that group's error rate drops in
comparable sessions (same model, effort, and Claude Code version). If
neither happens, the whole pipeline is pointless, and we find that out in an
afternoon rather than a fortnight. Only after that: model-based checks, then
Jev, then proposals, then PRs.

Other numbers (turns per task, files edited and then edited again, runs of
consecutive errors, permission prompts, compactions) are **raw material**
for higher-level questions, not goals in themselves. They are cheap to add
as queries once the fact tables exist.

## Decisions so far

Recorded 2026-09-27.

1. **First signal: tool-call errors, grouped by likely cause** (environment
   or sandbox, harness, stale docs or skills). Other measures are raw
   material, not targets.
2. **Record the context of every session**: model, effort level, Claude
   Code version. Group every before/after comparison by them.
3. **Git integration is expected.** The loop needs to know which version of
   CLAUDE.md, skills, and docs was in effect for each session. That comes
   from the repo's git history, which the add-on reads independently of CCL.
   CCL already resolves commit SHAs per repo (`git_remote.py`).
4. **The prototype is an isolated directory in this repo.** It uses CCL
   internals freely, and what it actually uses becomes the API spec later.
5. **The ledger is a SQLite database, not files in git.** It is application
   data and potentially sensitive.
6. **A separate CLI, run by cron or by hand, doing incremental work**, which
   then triggers further steps depending on the query results. Not part of
   `watch`: that only makes sense if we start acting on live sessions, and
   we shouldn't yet.
7. **No redaction before calling a model API.** If redaction mattered that
   much we'd be using a local model for everything. A secret-scanning
   library becomes necessary later, for sharing with a team or hosting in
   the cloud.
8. **All thresholds and limits are settings** with sensible defaults.
9. **Fact tables plus SQL, not precomputed counters.** The only real
   processing is getting data out of the transcripts. Tool inputs, results,
   and errors sit in the cache's compressed `content` blob, so SQL can't
   reach them directly. Once they're in flat tables, every "counter" is
   just a query.
10. **Causes come from the stages, not from rules alone.** Code extracts
    and clusters errors by signature; Jev classifies each cluster; a
    frontier model reads what's left, and may go back to the full
    transcript to investigate. Every investigation's evidence is recorded
    for a human reviewer or a later agent.
11. **An error is an error.** Each failed call is one row and counts once.
    The session is just one dimension to group by or drill into, like
    model or project, not a unit of counting.
12. **Cluster using DAG neighbours, not just error text**: where the
    failing argument came from, what fixed it, what happened next. This
    is cheap, and it separates *stale instructions* from *guessed* from
    *misled by the environment* before any model is involved.
13. **Evidence is pointers, not copies**: message UUIDs, git
    `(commit, path)`, and content hashes of snapshots for mutable files
    outside git. Transcripts are all kept, so pointers stay valid.
14. **Recurring fixes generate proposals.** When the same "what fixed it"
    pattern recurs across sessions, that's a proposal. *What kind* of
    change it becomes (a CLAUDE.md note, a skill, an environment fix
    such as putting a tool on PATH) is decided downstream, when the
    proposal is written, not at detection time.
15. **Logging what the harness injects is a prerequisite**, not an
    investigation for later. See the section above *Open questions*.

## Prerequisite: what the harness shows the agent but doesn't log

The "where did the failing argument come from" check only sees text the
agent *read with a tool*. Anything the harness injects directly is
invisible to it, so an argument taken from CLAUDE.md or memory would be
wrongly labelled "guessed". That makes this a **prerequisite for stage 1**,
not a side investigation. It is also **useful to base CCL on its own**:
anyone reading a transcript benefits from seeing what the agent had been
told.

**Step 1: inventory.** List everything the harness loads into context and
check, for each, whether the transcript records it and in what form.
Candidates:

- CLAUDE.md at every level: user (`~/.claude/CLAUDE.md`), project,
  `CLAUDE.local.md`, nested directories loaded on demand, and `@imports`
  inside them.
- Memory: the index loaded at start, and topic files read later.
- The skills listing (already logged as a `skill_listing` attachment),
  deferred tools (`deferred_tools_delta`), and MCP server instructions.
- Hook output (logged as `hook_*` attachments), output styles, settings
  that change behaviour (permissions, sandbox, effort level).

Scan a real archive for every `attachment.type` value and every
`system`/meta entry to see what is already there.

**Step 2: attach what's missing.** For each unlogged source, reconstruct it
from the session's timestamp and `cwd`:

- repo files → git: `(commit, path)` at the session's start;
- files under `~/.claude` → a `snapshots` row, by content hash.

Reconstruction from timestamps is a *best guess*: a file edited during the
session, or uncommitted changes, can make it wrong. So every attached item
records how it was obtained (logged | git | snapshot-by-time) and how sure
we are.

**Step 3 (optional, going forward): record it exactly.** A `SessionStart`
hook could write the paths and content hashes of everything loaded into a
side file keyed by session id. It costs almost nothing and turns future
guesses into facts, though it can't help the existing archive.

**Smaller open items:**

- **Where is the effort level recorded?** It isn't in the test fixtures.
  Check real transcripts and Claude Code's settings and changelog.
- **Are memory files append-only?** Probably not: as far as I know, Claude
  Code edits memory files in place (an index file plus topic files the
  agent rewrites). Confirm on a real `~/.claude`; the `snapshots` table
  exists because of this.

## Open questions

- **What does "improve" mean, as a number?** v0 takes a narrow view (errors
  by cause). The broader worry remains: the improvement that matters may
  only be recognisable by feel. In that case the honest design is a
  proposal queue with measurement as a safety check, not a closed loop. If
  so, excerpts earn their keep by *showing* evidence to a human more than by
  *proving* an effect.
- **Per-project or global queues?** Skills and stale docs belong to a
  project. Verbosity preferences and tool-usage habits belong to the user.
  Probably both, on different schedules.
- **Do Jev's confidence figures hold on our data?** Only trust the
  thresholds once they're checked against the labelled set. Until then,
  treat the probability as a ranking, not a decision.
- **How are checks written and reviewed?** A question that compiles into a
  SQL query plus a Jev checklist plus a prompt is a much better thing to
  keep than a bare prompt: it can be versioned, diffed, and tested. But
  someone has to review the generated query. How much review is enough?
- **Prototype directory name and CLI entry point.** For example
  `prototypes/self_improve/` with its own script, so it can't be mistaken
  for part of CCL's CLI.

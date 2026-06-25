# Cutting pytest-xdist worker-spawn / re-import cost on Windows

Status: WIP investigation (branch `dev/xdist-import-cost`).
Scope: conftest / test-module import cost + pyproject/CI xdist knobs.
Out of scope (parallel work by alice): `claude_code_log/cache.py`,
`test/test_integration_realistic.py`.

## Problem

Windows CI is ~3x slower than Linux CI, orthogonal to data volume. Linux
`fork()`s workers (copy-on-write, no re-import); Windows **spawns** a fresh
interpreter per xdist worker and **re-imports the whole world** in each. So any
module pulled in during collection is paid once per worker, every run.

## Root cause found

`pytest` imports **every** test module during collection and only *then*
applies `-m` marker deselection. The unit run (`-m "not (tui or browser ...)"`)
therefore imported the heavy optional deps for nothing:

- 10 `*_browser.py` modules `import playwright` at top level
- 2 `test_tui*.py` modules `import textual` at top level

Per-process cold import cost (fresh interpreter, measured locally):

| dep | import cost |
|-----|-------------|
| `textual` / `textual.app` | ~730–880 ms |
| `playwright.sync_api` | ~500 ms |
| `pytest_playwright` plugin (pulls playwright) | ~975 ms |

Key nuance: **playwright is loaded by the `pytest_playwright` plugin on every
worker anyway** (entry-point autoload), independent of whether the browser test
modules are collected. So skipping the browser modules does *not* save the
playwright import — the plugin already pays it. What the module-skip genuinely
removes from unit workers is **`textual`** (no always-on textual plugin).
Eliminating the playwright cost on unit runs is a *separate* lever: disabling
the unused plugin.

## Change 1 — don't import browser/TUI modules during unit collection (DONE)

`test/conftest.py` adds a `pytest_ignore_collect` hook that skips
`*_browser.py` / `test_tui*.py` when their marker can't be selected by the
active `-m` expression. It uses pytest's own `Expression` evaluator, so the
decision exactly tracks `-m` (no string-matching heuristics).

Verified selection is unchanged:

| run | before | after |
|-----|--------|-------|
| `-m "not (tui or browser)"` collected | 2214 | 2214 (modules skipped, not deselected) |
| `-m browser` collected | 62 | 62 |
| `-m tui` collected | 68 | 68 |
| no `-m` (full) collected | 2344 | 2344 |

Effect: removes the per-worker `textual` import (~0.7–0.9 s/worker) from every
unit run. Saving scales with worker count and is paid on **every** CI run
because each Windows worker re-imports.

## Change 2 — disable the unused `pytest_playwright` plugin on unit runs (PROPOSED)

Add `-p no:playwright` to the unit-test invocations (justfile `test`, CI "Run
unit tests" step). Removes ~0.97 s/worker of playwright import that unit tests
never use. Safe because the browser fixtures in conftest are lazy — nothing
requests `page`/`context` on a unit run (and browser modules are now skipped).
Must NOT go in `pyproject` `addopts` (that would disable it for the browser run
too); it belongs at the unit call sites only.

## Change 3 — xdist worker count (RECOMMENDATION, needs CI validation)

`pyproject` forces `-n auto --dist=worksteal`. On a 12-core dev box `-n auto`
spawns 12 workers, exaggerating the spawn+reimport tax (1 trivial test:
`-n0` ≈ 1.4 s vs `-n auto` ≈ 6.4 s). **But GitHub-hosted `windows-latest` has
only ~4 vCPUs**, so CI already runs `-n auto` ≈ 4 workers — modest. Cutting
worker count below the core count on CI would sacrifice real parallelism on the
slow integration tests for little spawn-cost gain.

Recommendation: **keep `-n auto`**; close the gap via per-worker import cost
(Changes 1+2), not by capping workers. The 12-worker tax is a dev-machine
artifact, not the CI reality.

> **Needs real-runner confirmation:** the definitive worker-count sweet spot
> must be measured on the actual GitHub Windows runner (fewer cores, different
> disk/AV profile than a dev box). Do not hard-commit a CI `-n` value from
> local numbers. If Windows CI is still slow after Changes 1+2, A/B `-n auto`
> vs a fixed `-n 2`/`-n 3` *on the runner* before changing the knob.

## Before/after (local measurements)

Box: Windows 11, 12 cores, warm caches. **Caveat:** every `uv run pytest`
invocation carries ~3 s of `uv` env-resolution overhead, which is included in
all wall numbers below and dilutes the measured delta. The deterministic
per-process import costs (above) are the cleaner signal for what each fresh CI
worker stops paying.

Parallel **collect-only** wall (`pytest --co`, forces every worker to
spawn + import + collect, no test execution), best-of-N to suppress contention:

| config | wall |
|--------|------|
| baseline (no hook, playwright loaded), `-n auto` | 4.2 s (best-of-5) |
| **complete change** (hook + `-p no:playwright`), `-n auto` | **3.1 s (best-of-5)** |
| baseline, `-n4` (CI-like core count) | 3.9 s (best-of-3) |
| after hook, `-n4` | 3.3 s (best-of-3) |

~26% off collect-only wall at `-n auto` despite the fixed ~3 s `uv` floor — i.e.
the spawn+import portion itself dropped substantially. `-p no:playwright` alone
accounts for ~1.4 s of it (`-n auto`: 5.0 s → 3.6 s in a separate best-of-3).

**Full unit-suite wall** (`-m "not (tui or browser)"`, ~2200 tests) measured
182 s / 237 s / 295 s across runs — dominated by the slow integration tests and
too contention-sensitive on this box to isolate a per-worker *startup* delta of
~1–2 s. This is expected: the startup saving is a fixed offset, swamped by test
execution. It surfaces best on the cold, fewer-core GitHub Windows runner —
hence the "needs CI confirmation" flag.

## Validated locally vs needs CI

- Validated locally: collection-selection equivalence; per-process import costs;
  conftest hook correctness across `-m` variants; suite stays green
  (except 3 pre-existing Windows symlink-privilege failures, below).
- Needs CI confirmation: real wall-clock delta on the GitHub Windows runner;
  the worker-count recommendation.

## Pre-existing unrelated failures

`test/test_session_scan_characterization.py::TestIndexInlineAggregateLoop...`
(3 tests) fail locally with `OSError [WinError 1314] A required privilege is not
held by the client` from `os.symlink` — a Windows Developer-Mode/admin
requirement, not related to import cost or this branch. Deselected in the A/B
measurements for a clean comparison.

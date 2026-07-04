# Windows runner I/O: why TEMP is redirected in CI

Status: concluded experiment; the fix (TMP/TEMP → `RUNNER_TEMP` on
Windows) lives in `.github/workflows/ci.yml`. This file is the evidence
record.

## Problem

The Windows CI unit-test step ran ~440–1100s for the same 2300-test
selection that takes ~160s on ubuntu-latest (and ~80s on a 12-core dev
box). Dropping coverage from non-primary jobs (PR #264) removed the
coverage tax but left a large multiple unexplained.

## Experiment

A/B run on real `windows-latest` runners (Python 3.12, 2 reps per
variant, `fail-fast: false`, only the pytest step timed; standalone
workflow on a throwaway branch, since deleted). Workflow run:
<https://github.com/daaain/claude-code-log/actions/runs/28715118956>

Unit selection: `pytest -p no:playwright -m "not (tui or browser or
benchmark)"`, no coverage.

| variant | what it changed | rep1 (s) | rep2 (s) | mean (s) |
|---|---|---|---|---|
| baseline | nothing (control) | 617.3 | 491.7 | 554.5 |
| defender-off | `Set-MpPreference -DisableRealtimeMonitoring $true` | 651.5 | 463.8 | 557.7 |
| defender-excl | `Add-MpPreference -ExclusionPath` workspace + temp + uv dirs | 535.0 | 810.4 | 672.7 |
| devdrive | ReFS dev drive (samypr100/setup-dev-drive), repo + TEMP + uv on it | 296.1 | 296.6 | 296.4 |
| **temp** | **TMP/TEMP → `$env:RUNNER_TEMP`** | **159.4** | **141.0** | **150.2** |

## Findings

1. **TMP/TEMP on the C: OS disk is the dominant cost.** The suite's
   heavy `tmp_path` / `TemporaryDirectory` traffic pays throttled-OS-disk
   prices. `RUNNER_TEMP` sits on the workspace disk (D:); redirecting is
   a 3.7× cut and lands at ubuntu parity (~161s). Both reps tight.
2. **Defender is a red herring.** `Get-MpComputerStatus` showed
   `RealTimeProtectionEnabled: False` on all 10 VMs — real-time
   protection is already off on current `windows-latest` images, so
   exclusions and disabling are no-ops. Their apparent spread is pure
   runner variance.
3. **A ReFS dev drive helps but is dominated** by the plain temp
   redirect (296s vs 150s) while adding a third-party action and drive
   sizing. Curiously repeatable timings (±0.5s between reps), though.
4. **Windows runner variance is ±30%** across identical jobs (464–810s
   for baseline-class variants). Single-rep Windows timings are not
   evidence; use ≥2 reps or variants with tight spreads.

## Resulting change

One early step in the test job, Windows-gated:

```yaml
- name: Move TEMP to the workspace disk (Windows)
  if: runner.os == 'Windows'
  shell: pwsh
  run: |
    "TMP=$env:RUNNER_TEMP" >> $env:GITHUB_ENV
    "TEMP=$env:RUNNER_TEMP" >> $env:GITHUB_ENV
```

Job-level (`GITHUB_ENV`) so every later step benefits — unit, TUI, and
browser tests all create temp files. Expected effect: the Windows unit
step drops from ~735s (last pre-experiment main run) to ~150s, taking
the Windows jobs off the CI critical path entirely.

## Non-obvious details for future experimenters

- `workflow_dispatch` on a branch-only workflow file does not work until
  the file reaches the default branch; trigger with `on: push` for the
  experiment branch and re-run via `gh run rerun` or empty commits.
- `setup-dev-drive` requires checkout **before** the action when using
  `workspace-copy: true`, and later actions' post-steps (e.g. cache
  save) run before its dismount only if the action is placed earlier in
  the step list.
- Probe `Get-MpComputerStatus` / `Get-Volume` in every job: image
  defaults change over time (Defender RT being off already is exactly
  the kind of fact that invalidates cargo-culted CI tweaks).

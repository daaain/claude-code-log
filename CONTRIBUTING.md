# Contributing to Claude Code Log

This guide covers development setup, testing, architecture, and release processes for contributors.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager

## Getting Started

```bash
git clone https://github.com/daaain/claude-code-log.git
cd claude-code-log
uv sync
```

## File Structure

```
claude_code_log/
├── cli.py              # Command-line interface with project discovery
├── tui.py              # Interactive Terminal User Interface (Textual)
├── parser.py           # Data extraction and parsing from JSONL files
├── renderer.py         # Format-neutral message processing and tree building
├── renderer_timings.py # Performance timing instrumentation
├── converter.py        # High-level conversion orchestration
├── models.py           # Pydantic models for transcript data structures
├── cache.py            # Cache management for performance optimization
├── factories/          # Transcript entry to MessageContent transformation
│   ├── meta_factory.py
│   ├── user_factory.py
│   ├── assistant_factory.py
│   ├── tool_factory.py
│   └── system_factory.py
├── html/               # HTML-specific rendering
│   ├── renderer.py
│   ├── user_formatters.py
│   ├── assistant_formatters.py
│   ├── system_formatters.py
│   ├── tool_formatters.py
│   └── utils.py
├── markdown/           # Markdown output rendering
│   └── renderer.py
└── templates/          # Jinja2 HTML templates
    ├── transcript.html
    ├── index.html
    └── components/
        └── timeline.html

scripts/                # Development utilities
test/test_data/         # Representative JSONL samples
dev-docs/               # Architecture / dev documentation (start in application_model.md)
docs/                   # User-facing operations docs
work/                   # Plans, TODOs, in-flight design docs
```

## Development Setup

The project uses:

- Python 3.10+ with uv package management
- Click for CLI interface
- Textual for Terminal User Interface
- Pydantic for data modeling and validation
- Jinja2 for HTML template rendering
- mistune for Markdown rendering
- dateparser for natural language date parsing

### Dependency Management

```bash
# Add a new dependency
uv add textual

# Remove a dependency
uv remove textual

# Sync dependencies
uv sync
```

## Testing

The project uses a categorized test system to avoid async event loop conflicts.

### Test Categories

- **Unit Tests** (no mark): Fast, standalone tests
- **TUI Tests** (`@pytest.mark.tui`): Textual-based TUI tests
- **Browser Tests** (`@pytest.mark.browser`): Playwright-based browser tests
- **Snapshot Tests**: HTML regression tests using syrupy

### Running Tests

```bash
# Unit tests only (fast, recommended for development)
just test
# or: uv run pytest -m "not (tui or browser)" -v

# TUI tests (isolated event loop)
just test-tui

# Browser tests (requires Chromium)
just test-browser

# All tests in sequence
just test-all

# Tests with coverage
just test-cov
```

### Snapshot Testing

Snapshot tests detect unintended HTML output changes using [syrupy](https://github.com/syrupy-project/syrupy):

```bash
# Run snapshot tests (parallel mode is fine for read-only runs)
uv run pytest test/test_snapshot_html.py -v

# Update snapshots after intentional HTML changes
# IMPORTANT: run --snapshot-update with -n0 (see warning below)
uv run pytest test/test_snapshot_html.py -n0 --snapshot-update
```

> **`--snapshot-update` must run serially (`-n0`) — a guard now enforces
> this.** Syrupy and pytest-xdist misbehave when writing the shared `.ambr`
> files in parallel, on two observed occasions. Once, a raced update
> silently *truncated* ~6000 lines, leaving a structurally-broken file that
> still passed on the next read — a confirmed corruption. Separately, a
> parallel `--snapshot-update` with a stale `__pycache__` produced a diff in
> which an *untouched* fixture appeared to regenerate with structure it had
> never carried (fold-bar / children markup); re-run serially with a purged
> cache, the same operation was cleanly additive and that structure did not
> appear. The mechanism of the second case isn't pinned down (the large
> deletion count first quoted for it turned out to be alignment noise — see
> "Recognising the race" below), but an operation that makes an untouched
> fixture look different is dangerous regardless, and "vanished under `-n0`"
> is the reproducible part. Because `pyproject.toml` defaults to `-n auto`,
> this unsafe combination is the *default*, so a `conftest.py` guard
> (`pytest_configure`) now fails fast when `--snapshot-update` is combined
> with more than one xdist worker, pointing you at `-n0`. Ordinary parallel
> runs (no update) are unaffected — CI is untouched.

**Recognising the race in a diff.** The guard prevents the mistake going
forward, but you may still meet a suspicious `.ambr` diff — reviewing a PR
that carries snapshot changes, or reading a historical diff from before the
guard existed. The rule of thumb:

> A negative in an `.ambr` diff is a signal to **investigate**, not a
> verdict. A purely additive regeneration is `+N/-0`, so deletions mean one
> of three things: an intentional content change you can name, a benign
> realignment of shared boilerplate, or the race.

To tell which, check at the **block level**, not the raw git diff: compare
the set of snapshot names (a race removes or rewrites blocks you didn't
touch) and diff each block's content. Inserting a snapshot or changing
embedded CSS realigns shared boilerplate and can show hundreds of
"deletions" with **zero content lost** — that is the benign case, and it is
the one a raw `-N` most often turns out to be. A real `-275` was exactly this:
block-level inspection found one snapshot added, none removed or renamed,
and seven blocks each `+3` for a single `white-space: pre-wrap` rule, with
zero content deleted. Confirm either way with a read-only `-n0` run after
purging stale bytecode — if every snapshot passes, the committed file
matches what the code renders and is not a raced artifact:

```bash
find . -name __pycache__ -type d -prune -exec rm -rf {} +
uv run pytest test/test_snapshot_html.py -n0
# all pass → the committed .ambr matches the render (not a raced file)
```

When you do intend to regenerate, run `--snapshot-update` serially; a purely
additive result (`+N/-0`, e.g. "8 snapshots passed. 1 snapshot generated.")
is the healthy signature:

```bash
uv run pytest test/test_snapshot_html.py -n0 --snapshot-update
```

When snapshot tests fail:
1. Review the diff to verify changes are intentional
2. If intentional, run `--snapshot-update` (serially) to accept new output
3. If unintentional, fix your code and re-run tests

### Test Prerequisites

Browser tests require Chromium:

```bash
uv run playwright install chromium
```

### Why Test Categories?

The test suite is categorized because different async frameworks conflict:

- **TUI tests** use Textual's async event loop (`run_test()`)
- **Browser tests** use Playwright's internal asyncio
- **pytest-asyncio** manages async test execution

Running all tests together can cause "RuntimeError: This event loop is already running". The categorization ensures reliable test execution.

### Test Coverage

```bash
# Run with coverage
just test-cov

# Or manually:
uv run pytest --cov=claude_code_log --cov-report=html --cov-report=term
```

HTML coverage reports are generated in `htmlcov/index.html`.

### Testing Resources

- See [test/README.md](test/README.md) for comprehensive testing documentation
- Visual Style Guide: `uv run python scripts/generate_style_guide.py`
- Test data in `test/test_data/`

## Code Quality

```bash
# Format code
ruff format

# Lint and fix
ruff check --fix

# Type checking
uv run pyright
uv run ty check
```

### Whitespace

An [`.editorconfig`](.editorconfig) at the repo root defines the baseline —
UTF-8, LF line endings, a final newline, and trimmed trailing whitespace.
Most editors honour it automatically; please keep it observed. Two paths
deliberately opt out of trailing-whitespace trimming: Markdown (a line ending
in two spaces is a hard break) and syrupy `.ambr` snapshots (the serializer
indents blank lines, so regenerate them rather than trimming by hand).

## Performance Profiling

Enable timing instrumentation to identify bottlenecks:

```bash
CLAUDE_CODE_LOG_DEBUG_TIMING=1 claude-code-log path/to/file.jsonl
```

This outputs detailed timing for each rendering phase, plus hit rates for
the render memo caches. The timing module is in
`claude_code_log/renderer_timings.py`.

Pygments highlighting and Markdown rendering are memoized because every
message is formatted twice per run (once for its combined page, once for
its session file) — see `claude_code_log/render_cache.py` and
[dev-docs/application_model.md § 2.9](dev-docs/application_model.md).
Set `CLAUDE_CODE_LOG_RENDER_CACHE_MB=0` to disable memoization when
bisecting a rendering difference; any other value sets the per-cache byte
budget in MB (default 192).

Above the leaf memo, a per-conversion fragment store
(`claude_code_log/fragment_store.py`) reuses each message's complete
formatted fragment between the combined-page and per-session passes.
Set `CLAUDE_CODE_LOG_FRAGMENT_STORE=0` to disable it when bisecting;
see [dev-docs/application_model.md § 2.9](dev-docs/application_model.md)
for its correctness guards.

A project's own pages and session files are additionally rendered in
parallel worker processes, on by default at the CPU count. Set
`CLAUDE_CODE_LOG_RENDER_JOBS=1` (or `off`) to disable it, or an integer to
pin a worker count. It earns its keep on the runs that matter — an
incremental run over a real archive measured 93.2s → 34.6s on 16 cores —
at the cost of more total CPU, since each worker starts with a cold memo
cache. Workers are *fed*, not self-loading: each unit crosses the process
boundary carrying its own entry slice and (for session files) its slice
of the fragment store, so workers verify-and-reuse formatted fragments
instead of re-formatting, and no worker loads the project's transcript.
Small projects are excluded outright, and the worker count is capped
against available memory (the parent is charged its measured master-list
footprint, each fed worker only its measured slice-holding cost): on a
small machine or a large archive it degrades to serial rather than
swapping. See
[dev-docs/application_model.md § 2.10](dev-docs/application_model.md) for
the measurements.

To re-measure both on your own hardware (core count changes the answer for
the fan-out), point the benchmark at a real project:

```bash
uv run python scripts/bench_render.py ~/.claude/projects/<project>
```

It copies the project to scratch space, warms the cache, then times every
combination of the two knobs plus a worker-count sweep — and hashes the
output of each, so it doubles as an equivalence check across far more real
data than the test fixtures cover.

## Diagnosing Hangs

If `claude-code-log` appears stuck (100% CPU, no output), send `SIGUSR1` to print the live Python stack to stderr without killing the process:

```bash
# In another terminal
kill -USR1 $(pgrep -f claude-code-log | head -1)
```

The handler is installed in `cli.py` via `faulthandler.register(SIGUSR1)`. POSIX-only; no-op on Windows. Unlike `py-spy`, it needs no root and no extra install.

## Documentation Site

The project publishes a documentation site to GitHub Pages, built with
[MkDocs](https://www.mkdocs.org/) and the
[Material](https://squidfunk.github.io/mkdocs-material/) theme. The site
configuration is `mkdocs.yml`; pages live under `docs/`.

```bash
# Install docs dependencies
uv sync --group docs

# Live-reload preview at http://127.0.0.1:8000
just docs-serve

# Strict build (fails on broken links/nav — same as CI)
just docs-build
```

Key points:

- **CLI reference** (`docs/reference/cli.md`) is rendered live from the Click
  command via the `mkdocs-click` plugin — no manual upkeep.
- **TUI reference** (`reference/tui.md`) is generated at build time by
  `docs/gen_pages.py` (a `mkdocs-gen-files` script): it introspects the Textual
  `BINDINGS` for the keybindings tables (`scripts/generate_tui_docs.py`) and
  captures SVG screenshots of the running TUI
  (`scripts/generate_tui_screenshots.py`). Both scripts are runnable standalone.
- **Example output** (`example.md` + `examples/transcript.html`) is rendered at
  build time from a bundled sample project
  (`scripts/generate_example_output.py`, also `just example`) — no private data
  or release asset involved. Generation is fault-tolerant so a render hiccup
  can't block the build.
- **Development** section surfaces `dev-docs/` (symlinked as `docs/development`).
  `CONTRIBUTING.md` and `CHANGELOG.md` are symlinked in as `docs/contributing.md`
  and `docs/changelog.md`. A build hook (`docs/hooks.py`) rewrites links to repo
  source files (e.g. `../claude_code_log/cli.py`) into GitHub URLs so the strict
  build stays green.
- Deployment is automated by `.github/workflows/docs.yml`: PRs run a strict
  build; pushes to `main` deploy to Pages. The repo's **Settings → Pages →
  Source** must be set to **GitHub Actions** (one-time).

## Architecture

Start with [dev-docs/application_model.md](dev-docs/application_model.md)
for the system overview (subsystems, data lifecycle, glossary). For
the rendering pipeline specifically, see
[dev-docs/rendering-architecture.md](dev-docs/rendering-architecture.md).

### Data Flow Overview

```
JSONL File
    ↓ (parser.py)
list[TranscriptEntry]
    ↓ (factories/)
list[TemplateMessage] with MessageContent
    ↓ (renderer.py)
Tree of TemplateMessage (roots with children)
    ↓ (html/renderer.py or markdown/renderer.py)
Final output (HTML or Markdown)
```

### Data Models

The application uses Pydantic models to parse and validate transcript JSON data:

- **TranscriptEntry**: Union of User, Assistant, Summary, System, QueueOperation entries
- **UsageInfo**: Token usage tracking (input/output tokens, cache tokens)
- **ContentItem**: Union of Text, ToolUse, ToolResult, Thinking, Image content

### Template System

Uses Jinja2 templates for HTML generation:

- **Session Navigation**: Table of contents with timestamp ranges and token summaries
- **Message Rendering**: Handles different content types with appropriate formatting
- **Token Display**: Shows usage for individual messages and session totals

### Timeline Component

The interactive timeline is implemented in JavaScript within `claude_code_log/templates/components/timeline.html`. When adding new message types or modifying CSS class generation, ensure the timeline's message type detection logic is updated accordingly.

## Cache System

The tool implements a SQLite-based caching system for performance:

- **Location**: `claude-code-log-cache.db` in the projects directory (or set `CLAUDE_CODE_LOG_CACHE_PATH` env var)
- **Contents**: Pre-parsed session metadata (IDs, summaries, timestamps, token usage)
- **Invalidation**: Automatic detection based on file modification times
- **Performance**: 10-100x faster loading for large projects

The cache automatically rebuilds when source files change or cache schema version changes.

## Release Process

The project uses automated releases with semantic versioning.

### Quick Release

```bash
# Bump version and create release (patch/minor/major)
just release-prep patch    # Bug fixes
just release-prep minor    # New features
just release-prep major    # Breaking changes

# Or specify exact version
just release-prep 0.4.3

# Preview what would be released
just release-preview

# Push to PyPI and create GitHub release
just release-push
```

### GitHub Release Only

```bash
just github-release          # For latest tag
just github-release 0.4.2    # For specific version
```

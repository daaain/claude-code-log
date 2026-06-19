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

> **Warning — don't let `--snapshot-update` run with `-n auto`.** Syrupy
> and pytest-xdist race when writing snapshot files in parallel: the
> `.ambr` file ends up truncated (observed: ~6000 lines silently
> deleted on a single run, leaving the file structurally broken but
> still passing on next read). Run `--snapshot-update` serially.

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

## Performance Profiling

Enable timing instrumentation to identify bottlenecks:

```bash
CLAUDE_CODE_LOG_DEBUG_TIMING=1 claude-code-log path/to/file.jsonl
```

This outputs detailed timing for each rendering phase. The timing module is in `claude_code_log/renderer_timings.py`.

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

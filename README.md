# Claude Code Log

A Python CLI tool that converts Claude Code transcript JSONL files into readable HTML format.

Browser log demo:

[Browser log](https://github.com/user-attachments/assets/12d94faf-6901-4429-b4e6-ea5f102d0c1c)

TUI demo:

[TUI](https://github.com/user-attachments/assets/75718e2b-3b02-4e17-8f3d-366e2c40dcc2)

## Project Overview

📋 **[View Changelog](CHANGELOG.md)** - See what's new in each release

This tool generates clean, minimalist HTML pages showing user prompts and assistant responses chronologically. It's designed to create a readable log of your Claude Code interactions with support for both individual files and entire project hierarchies.

📄 **[View Example HTML Output](https://github.com/daaain/claude-code-log/releases/latest/download/claude-code-log-transcript.html)** - Download a real example generated from this project's development (large file, ~100 MB)

## Quickstart

TL;DR: run the command below and browse the pages generated from your entire Claude Code archives:

```sh
uvx claude-code-log@latest --open-browser
```

## Key Features

- **Multiple Output Formats**: Generate HTML, plain text, markdown, or compact chat output from transcript files
- **Interactive TUI (Terminal User Interface)**: Browse and manage Claude Code sessions with real-time navigation, summaries, and quick actions for HTML export and session resuming
- **Project Hierarchy Processing**: Process entire `~/.claude/projects/` directory with linked index page
- **Individual Session Files**: Generate separate HTML files for each session with navigation links
- **Single File or Directory Processing**: Convert individual JSONL files or specific directories
- **Session Navigation**: Interactive table of contents with session summaries and quick navigation
- **Token Usage Tracking**: Display token consumption for individual messages and session totals
- **Runtime Message Filtering**: JavaScript-powered filtering to show/hide message types (user, assistant, system, tool use, etc.)
- **Chronological Ordering**: All messages sorted by timestamp across sessions
- **Interactive timeline**: Generate an interactive, zoomable timeline grouped by message times to navigate conversations visually
- **Cross-Session Summary Matching**: Properly match async-generated summaries to their original sessions
- **Date Range Filtering**: Filter messages by date range using natural language (e.g., "today", "yesterday", "last week")
- **Rich Message Types**: Support for user/assistant messages, tool use/results, thinking content, images
- **System Command Visibility**: Show system commands (like `init`) in expandable details with structured parsing
- **Markdown Rendering**: Server-side markdown rendering with syntax highlighting using mistune
- **Floating Navigation**: Always-available back-to-top button and filter controls
- **CLI Interface**: Simple command-line tool using Click

## What Problems Does This Solve?

This tool helps you answer questions like:

- **"How can I review all my Claude Code conversations?"**
- **"What did I work on with Claude yesterday/last week?"**
- **"How much are my Claude Code sessions costing?"**
- **"How can I search through my entire Claude Code history?"**
- **"What tools did Claude use in this project?"**
- **"How can I share my Claude Code conversation with others?"**
- **"What's the timeline of my project development?"**
- **"How can I analyse patterns in my Claude Code usage?"**

## Usage

### Interactive TUI (Terminal User Interface)

The TUI provides an interactive interface for browsing and managing Claude Code sessions with real-time navigation, session summaries, and quick actions.

```bash
# Launch TUI for all projects (default behavior)
claude-code-log --tui

# Launch TUI for specific project directory
claude-code-log /path/to/project --tui

# Launch TUI for specific Claude project
claude-code-log my-project --tui  # Automatically converts to ~/.claude/projects/-path-to-my-project
```

**TUI Features:**

- **Session Listing**: Interactive table showing session IDs, summaries, timestamps, message counts, and token usage
- **Smart Summaries**: Prioritizes Claude-generated summaries over first user messages for better session identification
- **Working Directory Matching**: Automatically finds and opens projects matching your current working directory
- **Quick Actions**:
  - `h` or "Export to HTML" button: Generate and open session HTML in browser
  - `c` or "Resume in Claude Code" button: Continue session with `claude -r <sessionId>`
  - `r` or "Refresh" button: Reload session data from files
  - `p` or "Projects View" button: Switch to project selector view
- **Project Statistics**: Real-time display of total sessions, messages, tokens, and date range
- **Cache Integration**: Leverages existing cache system for fast loading with automatic cache validation
- **Keyboard Navigation**: Arrow keys to navigate, Enter to expand row details, `q` to quit
- **Row Expansion**: Press Enter to expand selected row showing full summary, first user message, working directory, and detailed token usage

### Default Behavior (Process All Projects)

```bash
# Process all projects in ~/.claude/projects/ (default behavior)
claude-code-log

# Explicitly process all projects
claude-code-log --all-projects

# Process all projects and open in browser
claude-code-log --open-browser

# Process all projects with date filtering  
claude-code-log --from-date "yesterday" --to-date "today"
claude-code-log --from-date "last week"

# Skip individual session files (only create combined transcripts)
claude-code-log --no-individual-sessions
```

This creates:

- `~/.claude/projects/index.html` - Top level index with project cards and statistics
- `~/.claude/projects/project-name/combined_transcripts.html` - Individual project pages (these can be several megabytes)
- `~/.claude/projects/project-name/session-{session-id}.html` - Individual session pages

### Single File or Directory Processing

```bash
# Single file
claude-code-log transcript.jsonl

# Specific directory
claude-code-log /path/to/transcript/directory

# Custom output location
claude-code-log /path/to/directory -o combined_transcripts.html

# Open in browser after conversion
claude-code-log /path/to/directory --open-browser

# Filter by date range (supports natural language)
claude-code-log /path/to/directory --from-date "yesterday" --to-date "today"
claude-code-log /path/to/directory --from-date "3 days ago" --to-date "yesterday"
```

### Text and Markdown Output

Convert transcripts to plain text or markdown format for documentation or terminal viewing:

```bash
# Generate plain text output (verbose with timestamps, token usage)
claude-code-log /path/to/directory --format text -o output.txt

# Generate markdown output
claude-code-log /path/to/directory --format markdown -o output.md

# Generate compact chat format (clean conversation flow)
claude-code-log /path/to/directory --format chat -o chat.txt

# Single file with chat format (most readable)
claude-code-log transcript.jsonl --format chat
```

**Format Comparison:**

- **text**: Verbose format with timestamps, token usage, working directories, and full tool details
- **markdown**: Same as text but with markdown heading hierarchy for better document integration
- **chat**: Compact format mimicking Claude Code UI - clean conversation flow with tool use symbols (⏺) and truncated results (⎿)

**All Format Features:**
- Session headers with IDs and summaries (text/markdown only)
- User and assistant message separation
- Tool use and tool result rendering
- Thinking content blocks (text/markdown only)
- Chat format: clean, minimal output perfect for quick review

## File Structure

- `claude_code_log/parser.py` - Data extraction and parsing from JSONL files
- `claude_code_log/renderer.py` - HTML generation and template rendering
- `claude_code_log/text_renderer.py` - Plain text and markdown rendering
- `claude_code_log/converter.py` - High-level conversion orchestration
- `claude_code_log/cli.py` - Command-line interface with project discovery
- `claude_code_log/models.py` - Pydantic models for transcript data structures
- `claude_code_log/templates/` - Jinja2 HTML templates
  - `transcript.html` - Main transcript viewer template
  - `index.html` - Project directory index template
- `pyproject.toml` - Project configuration with dependencies

## Development

The project uses:

- Python 3.10+ with uv package management
- Click for CLI interface and argument parsing
- Pydantic for robust data modeling and validation
- dateparser for natural language date parsing
- Standard library for JSON/HTML processing
- Minimal dependencies for portability
- mistune for quick Markdown rendering

## Development Commands

### Testing

The project uses a categorized test system to avoid async event loop conflicts between different testing frameworks:

#### Test Categories

- **Unit Tests** (no mark): Fast, standalone tests with no external dependencies
- **TUI Tests** (`@pytest.mark.tui`): Tests for the Textual-based Terminal User Interface
- **Browser Tests** (`@pytest.mark.browser`): Playwright-based tests that run in real browsers

#### Running Tests

```bash
# Run only unit tests (fast, recommended for development)
uv run pytest -m "not (tui or browser)"

# Run TUI tests (isolated event loop)
uv run pytest -m tui

# Run browser tests (requires Chromium)
uv run pytest -m browser

# Run all tests in sequence (separated to avoid conflicts)
uv run pytest -m "not tui and not browser"; uv run pytest -m tui; uv run pytest -m browser
```

#### Prerequisites

Browser tests require Chromium to be installed:

```bash
uv run playwright install chromium
```

#### Why Test Categories?

The test suite is categorized because:

- **TUI tests** use Textual's async event loop (`run_test()`)
- **Browser tests** use Playwright's internal asyncio
- **pytest-asyncio** manages async test execution

Running all tests together can cause "RuntimeError: This event loop is already running" conflicts. The categorization ensures reliable test execution by isolating different async frameworks.

### Test Coverage

Generate test coverage reports:

```bash
# Run tests with coverage
uv run pytest --cov=claude_code_log --cov-report=html --cov-report=term

# Generate HTML coverage report only
uv run pytest --cov=claude_code_log --cov-report=html

# View coverage in terminal
uv run pytest --cov=claude_code_log --cov-report=term-missing
```

HTML coverage reports are generated in `htmlcov/index.html`.

**Comprehensive Testing & Style Guide**: The project includes extensive testing infrastructure and visual documentation. See [test/README.md](test/README.md) for details on:

- **Unit Tests**: Template rendering, message type handling, edge cases
- **Test Coverage**: 78%+ coverage across all modules with detailed reporting
- **Visual Style Guide**: Interactive documentation showing all message types
- **Representative Test Data**: Real-world JSONL samples for development
- **Style Guide Generation**: Create visual documentation with `uv run python scripts/generate_style_guide.py`

### Code Quality

- **Format code**: `ruff format`
- **Lint and fix**: `ruff check --fix`
- **Type checking**: `uv run pyright` and `uv run ty check`

### All Commands

- **Test (Unit only)**: `uv run pytest`
- **Test (TUI)**: `uv run pytest -m tui`
- **Test (Browser)**: `uv run pytest -m browser`
- **Test (All categories)**: `uv run pytest -m "not tui and not browser"; uv run pytest -m tui; uv run pytest -m browser`
- **Test with Coverage**: `uv run pytest --cov=claude_code_log --cov-report=html --cov-report=term`
- **Format**: `ruff format`
- **Lint**: `ruff check --fix`
- **Type Check**: `uv run pyright` and `uv run ty check`
- **Generate Style Guide**: `uv run python scripts/generate_style_guide.py`

Test with Claude transcript JSONL files typically found in `~/.claude/projects/` directories.

## Release Process (For Maintainers)

The project uses an automated release process with semantic versioning. Here's how to create and publish a new release:

### Quick Release

```bash
# Bump version and create release (patch/minor/major)
just release-prep patch    # For bug fixes
just release-prep minor    # For new features
just release-prep major    # For breaking changes

# Or specify exact version
just release-prep 0.4.3

# Preview what would be released
just release-preview

# Push to PyPI and create GitHub release
just release-push
```

3. **GitHub Release Only**: If you need to create/update just the GitHub release:

   ```bash
   just github-release          # For latest tag
   just github-release 0.4.2    # For specific version
   ```

### Cache Structure and Benefits

The tool implements a sophisticated caching system for performance:

- **Cache Location**: `.cache/` directory within each project folder
- **Session Metadata**: Pre-parsed session information (IDs, summaries, timestamps, token usage)
- **Timestamp Index**: Enables fast date-range filtering without parsing full files
- **Invalidation**: Automatic detection of stale cache based on file modification times
- **Performance**: 10-100x faster loading for large projects with many sessions

The cache is transparent to users and automatically rebuilds when:

- Source JSONL files are modified
- New sessions are added
- Cache structure version changes

## Project Hierarchy Output

When processing all projects, the tool generates:

```sh
~/.claude/projects/
├── index.html                           # Master index with project cards
├── project1/
│   ├── combined_transcripts.html        # Combined project page
│   ├── session-{session-id}.html        # Individual session pages
│   └── session-{session-id2}.html       # More session pages...
├── project2/
│   ├── combined_transcripts.html
│   └── session-{session-id}.html
└── ...
```

### Index Page Features

- **Project Cards**: Each project shown as a clickable card with statistics
- **Session Navigation**: Expandable session list with summaries and quick access to individual session files
- **Summary Statistics**: Total projects, transcript files, and message counts with token usage
- **Recent Activity**: Projects sorted by last modification date
- **Quick Navigation**: One-click access to combined transcripts or individual sessions
- **Clean URLs**: Readable project names converted from directory names

## Message Types Supported

- **User Messages**: Regular user inputs and prompts
- **Assistant Messages**: Claude's responses with token usage display
- **Summary Messages**: Session summaries with cross-session matching
- **System Commands**: Commands like `init` shown in expandable details with structured parsing
- **Tool Use**: Tool invocations with collapsible details and special TodoWrite rendering
- **Tool Results**: Tool execution results with error handling
- **Thinking Content**: Claude's internal reasoning processes
- **Images**: Pasted images and screenshots

## HTML Output Features

- **Responsive Design**: Works on desktop and mobile
- **Runtime Message Filtering**: JavaScript controls to show/hide message types with live counts
- **Session Navigation**: Interactive table of contents with session summaries and timestamp ranges
- **Token Usage Display**: Individual message and session-level token consumption tracking
- **Syntax Highlighting**: Code blocks properly formatted with markdown rendering
- **Markdown Support**: Server-side rendering with mistune including:
  - Headers, lists, emphasis, strikethrough
  - Code blocks and inline code
  - Links, images, and tables
  - GitHub Flavored Markdown features
- **Collapsible Content**: Tool use, system commands, and long content in expandable sections
- **Floating Controls**: Always-available filter button, details toggle, and back-to-top navigation
- **Cross-Session Features**: Summaries properly matched across async sessions

## Installation

Install using pip:

```bash
pip install claude-code-log
```

Or run directly with uvx (no separate installation step required):

```bash
uvx claude-code-log@latest
```

Or install from source:

```bash
git clone https://github.com/daaain/claude-code-log.git
cd claude-code-log
uv sync
uv run claude-code-log
```

## TODO

- tutorial overlay
- integrate `claude-trace` request logs if present?
- Localised number formatting and timezone adjustment runtime? For this we'd need to make Jinja template variables more granular
- convert images to WebP as screenshots are often huge PNGs – this might be time consuming to keep redoing (so would also need some caching) and need heavy dependencies with compilation (unless there are fast pure Python conversation libraries? Or WASM?)
- add special formatting for built-in tools: Bash, Glob, Grep, LS, exit_plan_mode, Read, Edit, MultiEdit, Write, NotebookRead, NotebookEdit, WebFetch, TodoRead, TodoWrite, WebSearch
- do we need to handle compacted conversation?
- Thinking block should have Markdown rendering as sometimes they have formatting
- system blocks like `init` also don't render perfectly, losing new lines
- add `ccusage` like daily summary and maybe some textual summary too based on Claude generate session summaries?
– import logs from @claude Github Actions
- stream logs from @claude Github Actions, see [octotail](https://github.com/getbettr/octotail)
- wrap up CLI as Github Action to run after Cladue Github Action and process [output](https://github.com/anthropics/claude-code-base-action?tab=readme-ov-file#outputs)
- extend into a VS Code extension that reads the JSONL real-time and displays stats like current context usage and implements a UI to see messages, todos, permissions, config, MCP status, etc
- feed the filtered user messages to headless claude CLI to distill the user intent from the session
- filter message type on Python (CLI) side too, not just UI
- Markdown renderer
- figure out minimum Python version and introduce a testing matrix
- add minimalist theme and make it light + dark; animate gradient background in fancy theme
- do we need special handling for hooks?

# Claude Code Log

A Python CLI tool that converts AI coding assistant transcripts into readable HTML and Markdown formats.

**Supported providers:** Claude Code, Codex CLI, Gemini CLI, OpenCode, Antigravity CLI (agy)

Browser log demo:

[Browser log](https://github.com/user-attachments/assets/12d94faf-6901-4429-b4e6-ea5f102d0c1c)

TUI demo:

[TUI](https://github.com/user-attachments/assets/75718e2b-3b02-4e17-8f3d-366e2c40dcc2)

## Project Overview

📋 **[View Changelog](CHANGELOG.md)** - See what's new in each release

This tool generates clean, minimalist HTML pages showing user prompts and assistant responses chronologically. It's designed to create a readable log of your AI coding assistant interactions with support for multiple providers (Claude Code, Codex CLI, Gemini CLI, OpenCode, Antigravity CLI) and both individual files and entire project hierarchies.

📄 **[View Example HTML Output](https://daaain.github.io/claude-code-log/example/)** - A real example generated from a sample of this project's development, regenerated on every docs build

## Quickstart

TL;DR: run the command below and browse the pages generated from all your AI coding assistant archives:

```sh
uvx claude-code-log@latest --open-browser
```

## Key Features

- **Multi-Provider Support**: Works with Claude Code, Codex CLI, Gemini CLI, OpenCode, and Antigravity CLI (agy) sessions
- **Interactive TUI (Terminal User Interface)**: Browse and manage sessions across all providers with real-time navigation, summaries, and quick actions for HTML export and session resuming
- **Provider Filtering**: Filter sessions by provider in the TUI (press `f` to cycle)
- **Project Hierarchy Processing**: Process entire project directories with linked index page
- **Individual Session Files**: Generate separate HTML files for each session with navigation links
- **Single File or Directory Processing**: Convert individual JSONL files or specific directories
- **Session Navigation**: Interactive table of contents with session summaries and quick navigation
- **Token Usage Tracking**: Display token consumption for individual messages and session totals
- **Runtime Message Filtering**: JavaScript-powered filtering to show/hide message types (user, assistant, system, tool use, etc.)
- **Chronological Ordering**: All messages sorted by timestamp across sessions and providers
- **Interactive timeline**: Generate an interactive, zoomable timeline grouped by message times to navigate conversations visually
- **Cross-Session Summary Matching**: Properly match async-generated summaries to their original sessions
- **Date Range Filtering**: Filter messages by date range using natural language (e.g., "today", "yesterday", "last week")
- **Rich Message Types**: Support for user/assistant messages, tool use/results, thinking content, images
- **System Command Visibility**: Show system commands (like `init`) in expandable details with structured parsing
- **Markdown Rendering**: Server-side markdown rendering with syntax highlighting using mistune
- **Detail Levels & Compact Mode**: `--detail full|high|low|minimal|user-only` filters by verbosity and `--compact` merges repeated section headings — pairs well with `--format md` to feed past conversations back to an LLM for analysis or experience building
- **Floating Navigation**: Always-available back-to-top button and filter controls
- **CLI Interface**: Simple command-line tool using Click

## What Problems Does This Solve?

This tool helps you answer questions like:

- **"How can I review all my AI coding assistant conversations?"**
- **"What did I work on with [Claude/Codex/Gemini/agy] yesterday/last week?"**
- **"How much are my coding assistant sessions costing?"**
- **"How can I search through my entire coding history across all tools?"**
- **"What tools did my assistant use in this project?"**
- **"How can I share my coding conversation with others?"**
- **"What's the timeline of my project development?"**
- **"How can I analyse patterns in my coding assistant usage?"**
- **"How can I feed a past session back to an LLM for analysis or experience building?"**

## Usage

### Interactive TUI (Terminal User Interface)

The TUI provides an interactive interface for browsing and managing sessions across all supported providers with real-time navigation, session summaries, and quick actions.

```bash
# Launch TUI for all projects (default behavior)
claude-code-log --tui

# Launch TUI for specific project directory
claude-code-log /path/to/project --tui

# Launch TUI for specific project (auto-resolves ~/.claude/projects/ path)
claude-code-log my-project --tui
```

**TUI Features:**

- **Multi-Provider Session Listing**: Interactive table showing session IDs, provider badges, summaries, timestamps, message counts, and token usage
- **Provider Filter**: Press `f` to cycle through providers (All → Claude → Codex → Gemini → OpenCode → agy)
- **Smart Summaries**: Prioritizes assistant-generated summaries over first user messages for better session identification
- **Working Directory Matching**: Automatically finds and opens projects matching your current working directory
- **Quick Actions**:
  - `h`: Generate and open session HTML in browser
  - `m`: Generate and open session Markdown in browser
  - `v`: View session Markdown in embedded viewer (with table of contents)
  - `c`: Resume session in its native CLI (where supported)
  - `r`: Reload session data from files
  - `p`: Switch to project selector view
  - `H`/`M`/`V`: Force regenerate HTML/Markdown (hidden shortcuts for development)
- **Project Statistics**: Real-time display of total sessions, messages, tokens, and date range
- **Cache Integration**: Leverages existing cache system for fast loading with automatic cache validation
- **Keyboard Navigation**: Arrow keys to navigate, Enter to expand row details, `q to quit
- **Row Expansion**: Press Enter to expand selected row showing full summary, first user message, working directory, and detailed token usage

### Default Behavior (Process All Projects)

```bash
# Process all projects across all providers (default behavior)
claude-code-log

# Process all projects from a specific provider
claude-code-log --provider claude
claude-code-log --provider codex
claude-code-log --provider gemini
claude-code-log --provider opencode
claude-code-log --provider agy

# Process all projects and open in browser
claude-code-log --open-browser

# Process all projects with date filtering
claude-code-log --from-date "yesterday" --to-date "today"
claude-code-log --from-date "last week"

# Skip individual session files (only create combined transcripts)
claude-code-log --no-individual-sessions

# List available providers
claude-code-log --list-providers
```

This creates:

- `~/.claude/projects/index.html` - Top level index with project cards and statistics
- `~/.claude/projects/project-name/combined_transcripts.html` - Individual project pages (these can be several megabytes)
- `~/.claude/projects/project-name/session-{session-id}.html` - Individual session pages
- `~/.claude/projects/project-name/session-{session-id}.md` - Markdown versions (generated on-demand via TUI)

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

### Feeding Past Conversations to an LLM

The combination `--detail low --format md --compact` produces condensed Markdown suitable as context for an LLM to review or distill patterns from past work:

```bash
# Session → compact Markdown for LLM review
claude-code-log transcript.jsonl --detail low --format md --compact -o session.md

# Whole project history
claude-code-log /path/to/project --detail low --format md --compact
```

`--detail` levels (smallest → largest output):

- `user-only` — just user prompts and steering (useful as input to a downstream agent, e.g. building a requirements doc)
- `minimal` — user + assistant text only
- `low` — interaction-focused; keeps WebSearch, WebFetch, and Task (agent delegations) as key signals
- `high` — detailed but cleaned; drops system/hook noise
- `full` — everything (default)

`--compact` merges consecutive same-type sections in Markdown so runs of assistant responses share one heading instead of repeating `### 🤖 Assistant:` for each.

### Linking Commit SHAs

Plain `7c2e6f6`-shaped tokens in transcript prose get turned into clickable commit links when the SHA is reachable from a local remote-tracking branch. **github.com**, **gitlab.com**, and **bitbucket.org** work out of the box. For self-hosted forges (in-house GitLab, Gitea, Forgejo, …), supply a URL template via `--git-link`:

```bash
# Self-hosted GitLab
claude-code-log /path/to/transcript --git-link 'https://{host}/{path}/-/commit/{sha}'

# Same thing via env var (useful for TUI / repeated invocations)
export CLAUDE_CODE_LOG_GIT_LINK='https://{host}/{path}/-/commit/{sha}'
claude-code-log --tui
```

Placeholders: `{host}`, `{path}`, `{sha}`. The template fires only when the static map doesn't already know the host, so a mix of GitHub repos + self-hosted GitLab gets correct links from both. SHAs not reachable from any local remote-tracking ref render as plain text — local-only work-in-progress commits never produce broken links.

## Project Hierarchy Output

When processing all projects, the tool generates output under each provider's data directory:

```sh
# Claude Code
~/.claude/projects/
├── index.html                           # Master index with project cards
├── project1/
│   ├── combined_transcripts.html        # Combined project page
│   ├── session-{session-id}.html        # Individual session pages
│   ├── session-{session-id}.md          # Markdown version (on-demand via TUI)
│   └── session-{session-id2}.html       # More session pages...
└── ...

# Codex CLI
~/.codex/sessions/YYYY/MM/DD/
└── rollout-*.jsonl                      # Session rollout files

# Gemini CLI
~/.gemini/tmp/<project-hash>/chats/
└── session-*.jsonl                      # Session files

# OpenCode
~/.local/share/opencode/storage/
├── session/                             # Session metadata
├── message/                             # Messages per session
└── part/                                # Message parts

# Antigravity CLI (agy)
~/.gemini/antigravity-cli/
├── conversations/                       # SQLite databases
├── brain/<uuid>/.system_generated/logs/
│   └── transcript.jsonl                 # Human-readable transcript
└── history.jsonl                        # User input history
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
- **Assistant Messages**: AI responses with token usage display
- **Summary Messages**: Session summaries with cross-session matching
- **System Commands**: Commands like `init` shown in expandable details with structured parsing
- **Tool Use**: Tool invocations with collapsible details and special TodoWrite rendering
- **Tool Results**: Tool execution results with error handling
- **Thinking Content**: AI's internal reasoning processes (where available)
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

## Markdown Output Features

Markdown export provides a lightweight, portable alternative to HTML:

- **GitHub-Flavored Markdown**: Compatible with GitHub, GitLab, and other Markdown renderers
- **Hierarchical Structure**: Sessions organized with headers and collapsible details
- **Message Excerpts**: Section titles include message previews for quick navigation
- **Code Preservation**: Syntax highlighting hints via fenced code blocks
- **Embedded Viewer**: TUI includes built-in Markdown viewer with table of contents
- **Image Support**: Configurable image handling (placeholder, embedded base64, or referenced files)
- **`--compact` Mode**: Merge consecutive same-type section headings — most useful with `--detail low` or `minimal` where tool stripping creates runs of Assistant or User sections

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and architecture documentation.

## Community Extensions

Projects built on top of `claude-code-log`:

- **[archive-session](https://github.com/lifeinchords/claude-code-skills#archive-session-skill--slash-command--optional-hook)** by [@lifeinchords](https://github.com/lifeinchords). Wraps the CLI as three integration surfaces:
  - a Claude Code [Skill](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/skills/archive-session/SKILL.md)
  - a Claude Code slash [Command](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/commands/archive-session.md) `/archive-session` for explicit in-chat invocation
  - a Claude Code PreCompact [Hook](https://github.com/lifeinchords/claude-code-skills/blob/main/.claude/hooks/pre-compact-archive.sh) that auto-archives transcripts and subagent logs right before context compaction

Cross-platform (macOS and Windows/MSYS).

## TODO

- tutorial overlay
- integrate `claude-trace` request logs if present?
- convert images to WebP as screenshots are often huge PNGs – this might be time consuming to keep redoing (so would also need some caching) and need heavy dependencies with compilation (unless there are fast pure Python conversation libraries? Or WASM?)
- add special formatting for built-in tools: Glob, Grep, LS, MultiEdit, NotebookRead, NotebookEdit, WebFetch, TodoRead, WebSearch
- add `ccusage` like daily summary and maybe some textual summary too based on Claude generate session summaries?
– import logs from @claude Github Actions
- stream logs from @claude Github Actions, see [octotail](https://github.com/getbettr/octotail)
- wrap up CLI as Github Action to run after Cladue Github Action and process [output](https://github.com/anthropics/claude-code-base-action?tab=readme-ov-file#outputs)
- feed the filtered user messages to headless claude CLI to distill the user intent from the session
- filter message type on Python (CLI) side too, not just UI
- add minimalist theme and make it light + dark; animate gradient background in fancy theme
- do we need special handling for hooks?
- make processing parallel, currently we only use 1 CPU (core) and it's slow
- merge git worktree directories

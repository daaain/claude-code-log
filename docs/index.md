# Claude Code Log

A Python CLI tool that converts Claude Code transcript JSONL files into readable
**HTML** and **Markdown**, with an interactive **TUI** for browsing and managing
your sessions.

## Quickstart

Run the command below and browse pages generated from your entire Claude Code
archive:

```sh
uvx claude-code-log@latest --open-browser
```

Or launch the interactive terminal UI:

```sh
uvx claude-code-log@latest --tui
```

## What it does

`claude-code-log` reads the transcripts Claude Code stores under
`~/.claude/projects/` and turns them into clean, navigable logs so you can:

- Review and search all your Claude Code conversations
- See what you worked on yesterday, last week, or in a date range
- Track token usage and session costs
- Share a conversation as a self-contained HTML page
- Feed a past session back to an LLM as Markdown for analysis

## Key features

- **Interactive TUI** — browse, export, and resume sessions from your terminal
- **Project hierarchy processing** — convert your whole `~/.claude/projects/` tree with a linked index
- **Per-session HTML** — individual pages with navigation between sessions
- **Token usage tracking** — per-message and per-session totals
- **Runtime message filtering** — show/hide message types in the browser
- **Interactive timeline** — zoomable, click-to-scroll conversation navigation
- **Date range filtering** — natural language (`"today"`, `"last week"`)
- **Detail levels & compact mode** — `--detail` and `--compact` for LLM-friendly Markdown
- **Server-side Markdown** — syntax-highlighted rendering via mistune
- **Obsidian / vault output** — `--expand-paths` mirrors each project under its real
  on-disk path and defaults `--combined` to `no` (per-session files only); a prior
  combined run's files are left in place, so pass `--clear-output` to sweep them.
  Works for provider wholesale renders too (e.g. `--provider codex`)

## Where to go next

- **[CLI reference](reference/cli.md)** — every command-line option, generated from the source
- **[TUI keybindings](reference/tui.md)** — every keyboard shortcut and screenshots of the interface
- **[Restoring archived sessions](restoring-archived-sessions.md)** — recover sessions whose JSONL was deleted
- **[Development](development/application_model.md)** — architecture deep-dives for contributors
- **[Contributing](contributing.md)** — dev setup, testing, and release process

## Example output

📄 **[View example HTML output](example.md)** — a real page generated from a
sample of this project's own development, regenerated on every docs build.

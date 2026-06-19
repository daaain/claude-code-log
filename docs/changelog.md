# Changelog

All notable changes to claude-code-log will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [1.4.0] - 2026-06-03

### Changed

- **Fix sed**
- **Add MkDocs documentation site with live TUI reference (#197)**
- **Relax Textual constraint from `==` to `>=` (#196)**
- **Add `--version` flag to the CLI (#195)**
- **Fix AskUserQuestion result rendering + highlight chosen options (#180) (#189)**
- **Derive render_session_id from the SessionTree, not a loop variable (#190)**
- **Fix collapsible body overlapping preceding content in tool cards (#153) (#187)**
- **Extract compute_session_data + compute_project_aggregates (C9b) (#188)**
- **Add C9a characterization tests for session-scan call sites (#186)**
- **Route converter summary + ai-title extraction through shared helpers (#185)**
- **status: Wave B fully merged; Wave C kickoff (C8/C9a/C9b stacked, C10 dropped, decisions locked)**
- **Compute branch preview once from the DAG-line (#184)**
- **status: #184 fully validated (CI 11/11, CodeRabbit clean) — ready to merge**
- **docs: keep simplification status note self-contained to project scope**
- **status: correct #184 state; move GitHub CI/CodeRabbit ops to github guideline**
- **status: reverse-order stacked-PR lift recipe CONFIRMED on #184**
- **Factor session-header construction out of _render_messages (#183)**
- **status: #183/#184 rebased + CodeRabbit forced (#183 clean, #184 2 doc fixes); record @coderabbitai + reverse-order workarounds**
- **Dedup requestId tokens in pagination cache-miss fallback (#182)**
- **status: Wave B track complete — opp 7 PR #184 up, all monk-approved; add merge sequence**
- **status: note stacked-PR CI/CodeRabbit defers to merge-time**
- **status: opp 1 green (#182, awaiting merge); opp 6 #183 in review; opp 7 in progress**
- **status: opp 1 -> PR #182 (in review); opp 6 in progress**
- **Add live-status section to simplification plan**
- **Add converter/renderer simplification plan**
- **Move detail-visibility predicate onto MessageContent (#181)**
- **Sync rendering-architecture.md §5 with the current pipeline (#178)**
- **Extract inline junction-forward-link block into a named pass (#177)**
- **Co-locate the away-summary detail rule on AwaySummaryMessage (#176)**
- **Remove vestigial progress-chain parent repair (#175)**
- **plugins: dev-docs gaps + public helper API + ToolResult example (Phase 2) (#173)**
- **Implement unified plugin system from RFC #166 (#169)**
- **Render Read tool results with pygments via structured payload (closes #170) (#172)**
- **work/: triage against shipped main (#171)**
- **Always regenerate projects index so variant-flag toggles refresh links (#168)**
- **RFC: plugin system (unified message-transformer mechanism) (#166)**
- **Per-message timestamps in Markdown output (#160) (#165)**
- **Support non-GitHub forges via static map + `--git-link` fallback (#156) (#164)**
- **Obsidian-friendly output: --output dir + --expand-paths + --filter-path (#151) (#155)**
- **Linkify commit SHAs in rendered Markdown + HTML — closes #156 (#161)**
- **CSS clean-ups (issue #153) (#163)**
- **Cross-link TaskOutput / TaskUpdate headers back to their spawn (#154) (#158)**


## [1.3.0] - 2026-05-14

### Changed

- **Render ScheduleWakeup and Cron* tools (#148) (#152)**
- **Render hook attachment entries at FULL detail (#128) (#149)**
- **Style sidechain filter toggle with dashed border**
- **scrub_surrogates: handle high surrogate range (CR follow-up) (#150)**
- **Render the built-in Monitor tool with Task-end backlink (#142) (#147)**
- **Add support for ai-title and prefer it over legacy summary (#136)**
- **fix: add errors='replace' to read_text/write_text for Unicode safety (#139) (#146)**
- **Fix UnicodeEncodeError on JSONL with lone surrogates (#139) (#144)**
- **Use `--dist=worksteal` to speed up tests + move `-n auto` to config to make it default (#145)**
- **Fix/prevent dag cycle (#138)**
- **Render away_summary recap entries (#111) (#141)**
- **System info cosmetic improvements + chain-pairing fix (#137) (#140)**
- **dev-docs: introduce application_model.md as entry point, normalize naming, clean work/ (#134)**
- **export conversations to json (#36)**
- **Suppress noise in system-info messages (#129) (#133)**
- **Fix DAG cyclic-children hang and add SIGUSR1 stack dump (#135)**
- **Support async agents (#90) (#132)**
- **Robust within-session fork rendering: collapse parallel-tool_use forks, consistent labels (#131)**
- **Render user content as Markdown with raw fallback toggle (#119)**
- **Add --detail user-only level (#118)**
- **Pair Slash Command with User (slash command) (#126) (#127)**
- **Fold Skill name into tool_use title and drop the params row**
- **Fold Skill body into its tool_use block (#121)**
- **docs: add Community Extensions section (#120)**
- **Support teammates (#91): stitching + session headers + index (PR 3 of 3) (#125)**
- **Support teammates (#91): rendering (PR 2 of 3) (#122)**
- **Support teammates (#91): parsing + data model (draft) (#117)**


## [1.2.0] - 2026-04-19

### Changed

- **Preserve agentId anchors in parallel-Task stitch + tool-param UI fix (#115)**
- **Per-level output files for --detail and --compact (#114)**
- **Handle custom-title, agent-name, and agent-color transcript entry types (#113)**
- **Ignore 'last-prompt' message type (#112)**
- **Detail levels and compact rendering of conversations (#96)**
- **Skip PassthroughTranscriptEntry in _render_messages**
- **Integrate agent transcripts into the DAG (Phase C) (#99)**
- **Implement DAG-based message ordering (Phases A+B) (#97)**
- **Fix slow test hitting real ~/.claude/projects (5GB) (#109)**
- **feat: add --session-id flag for exporting a single  (#103)**
- **Fix search broken when HTML saved with different filename (#106)**
- **Add Grep tool renderer with pattern in title (#107)**
- **Fix TUI square bracket escaping issue (#105)**


## [1.1.1] - 2026-03-10

### Changed

- **Fix build cold start + format justfile**
- **fix: handle None level in SystemMessage title (#100)**


## [1.1.0] - 2026-03-06

### Changed

- **Fix WebSearch and WebFetch rendering in agent transcripts (#98)**
- **Fix fold-bar colors and System Hook alignment (#89)**
- **Add WebFetch tool renderer (#87)**
- **Merge pull request #83 from daaain/dev/websearch-tool-renderer**
- **Update some outdated docs + VS Code insists on these settings (#86)**
- **Fix double tab opening when clicking links in TUI MarkdownViewer**
- **Simplify WebSearch parser and improve rendering**
- **Use structured toolUseResult for WebSearch parsing**
- **Add analysis content support to WebSearch output**
- **Add documentation for implementing tool renderers**
- **Add WebSearch HTML and Markdown formatters**
- **Add WebSearch tool models and factory parser**
- **Fix snapshot + make sure snapshot order is stable**
- **Improve CSS layout to be responsive for mobile small screens (#77)**
- **Update pyright to 1.1.408 (#82)**
- **Support subagents directory structure (Claude Code 2.1.2+) (#80)**


## [1.0.0] - 2026-01-22

BREAKING CHANGE: cache is now using a SQLite database instead of JSON files!

This shouldn't change how the library works for you, but if you were using it in a custom way, some edge cases might break your setup.

### Changed

- **SQLite cache (#59)**
- **Integrate review feedback from #71 + MarkdownPreview in TUI (#75)**
- **Consolidate Rendering Architecture (#74)**
- **Add Markdown renderer (#71)**
- **HTML polish: tool titles, AskUserQuestion, fold bar, thinking borders (#70)**
- **Rename *Content to *Message and add ToolOutput/ToolUseMessage types (#69)**
- **Remove MessageModifiers (#68)**
- **Refactor content formatting to use dispatcher pattern (#67)**
- **Improve message styling consistency (#66)**
- **Fix user text message deduplication to keep best version (#65)**
- **Integrate coderabbit review suggestions for #63**
- **Remove content_html field from TemplateMessage (#63)**


## [0.9.0] - 2025-12-08

### Changed

- **Polish User Messages (#60)**
- **Extract user preferences from project's .vscode/settings.json. (#61)**
- **Filter out warmup messages + parse IDE tags for concise display in summaries (#57)**
- **Fix cross-session tool pairing on session resume (#56)**
- **Fix Parallel Sidechain Rendering (#54)**
- **CSS Styles Cleanup (#53)**
- **Fix test + lint issues (#55)**
- **Review and polish (0.8dev) (#51)**
- **Integration tests (#52)**
- **More Collapsible Content & Slash Command Support (#50)**
- **Support for Steering Messages and Sidechain Cleanup (#49)**
- **Fix Pygments Lexer Performance Bottleneck (#48)**
- **Foldable messages (#42)**
- **Add more Python versions to testing matrix + fixes for 3.14 (#40)**
- **Handle (but don't render) "queue-operation" + remove GH Pages workflow**
- **Update README link + faster rsync**


## [0.8.0] - 2025-11-08

### Changed

- **Regenerate HTML files + couple tiny changes**
- **Use Pygments to render files and code snippets (#39)**
- **Fix Unicode escape in tool use content rendering (#38)**
- **Introduce visual structure for the conversation and some specialized tool rendering (#37)**


## [0.7.0] - 2025-10-22

### Changed

- **Regenerate JSON + HTML with all the latest merged features**
- **Add image rendering support to tool result content (#32)**
- **Add query parameter support for message type filtering (#34)**
- **feat(search): add search functionality (#31)**


## [0.6.0] - 2025-10-22

### Changed

- **Fix tests on windows (#33)**
- **Remove broken Claude PR review**
- **Convert timestamps to user's local timezone in the browser (#29)**


## [0.5.1] - 2025-10-04

### Changed

- **Wire up JSONL ensure_fresh_cache with converter to ensure HTML updated on change (#27)**


## [0.5.0] - 2025-09-03

### Changed

- **Config + regenerate outputs**
- **Apply ANSI colour parsing to Claude's Bash tool call outputs + strip escape sequences for cursor movement and screen manipulation**
- **Render system and bash commands (#19)**
- **Prevent UnicodeEncodeError: surrogates not allowed – fixes #16**
- **Fix timezone-dependent test failures in template data tests (#18)**
- **Add official Claude Code GitHub Workflow [skip-review] (#15)**


## [0.4.4] - 2025-07-30

### Changed

- **Fix TUI project matching (#11)**
- **Update README.md with TUI demo**


## [0.4.3] - 2025-07-20

### Changed

- **Make it possible to get to project selector in TUI even if pwd is a project + Github releases + fixes (#8)**


## [0.4.2] - 2025-07-18

### Changed

- **Untangle spaghetti with cache and generation race conditions, so now index page is rendering correctly**
- **Reuse session first message preview creation to prevent inconsistency**
- **Add one hour after default timeline view to centre messages and make sure they aren't cut off in the right**


## [0.4.1] - 2025-07-17

### Changed

- **Fix TUI test**
- **Add expanded session info panel to TUI + clean up after TUI exit + fix project name regression + take 1000 instead of 500 chars of first user message**
- **Merge pull request #7 from bbatsell/patch-1**
- **Add `packaging` to main dependencies**
- **Silence cache fill output lines when launching TUI + run test suites individually to fix CI**


## [0.4.0] - 2025-07-16

### Changed

- **Implement TUI to open individual HTML pages for sessions and to resume them with CC**
- **Implement better path handling by reading cwd from messages + link to combined transcript from individual session pages + HTML versioning and command to clear them**
- **Add cache version compatibility checker to prevent it from invalidating after compatible version bumps**


## [0.3.4] - 2025-07-13

### Changed

- **Implement caching (writes processed JSON files into .claude project directories)**
- **Extend ToolUseResult to handle List[ContentItem] to support MCP tool results**
- **Power to Claude**
- **Add Claude Code OAuth workflows**


## [0.3.3] - 2025-07-05

### Changed

- **Hide groups in the timeline instead of items + bug fixes**
- **Get tooltip config working + improve rendering and styling**


## [0.3.2] - 2025-07-03

### Changed

- **Fix initial message lookup for session boxes + only show one hour of timeline to decrease initialisation time**
- **Fix lint issue**
- **Fix sidechain issues in timeline and add to filters + add Playwright browser testing**
- **Docs update**
- **Use Anthropic Python SDK for parsing types + handle sub-assistant and system messages**
- **Fix broken test + add ty and fix type errors**


## [0.3.1] - 2025-07-01

### Changed

- **Timeline tooltips + dead code cleanup**


## [0.3.0] - 2025-06-29

### Changed

- **Add timeline functionality**
- **Rewrite session starter prompt picking script and reuse between pages**
- **Pull out CSS to composable modules + add session list to index page + docs update**


## [0.2.9] - 2025-06-24

### Added

- **Individual Session Files**: Generate separate HTML files for each session with navigation links
- **Cross-Session Summary Matching**: Fixed async summary generation by properly matching summaries from later sessions to their original sessions
- **Session Navigation on Index Page**: Added expandable session lists with summaries and direct links to individual session files

### Fixed

- **Session Summary Display**: Session summaries now appear correctly on both index and transcript pages
- **Session Ordering**: Sessions now appear in ascending chronological order (oldest first) on index page to match transcript page
- **Type Safety**: Improved type checking consistency between index and transcript page processing

## [0.2.8] - 2025-06-23

### Added

- **Runtime Message Filtering**: JavaScript-powered filtering toolbar to show/hide message types
  - Toggle visibility for user, assistant, system, tool use, tool results, thinking, and image messages
  - Live message counts for each type
  - Select All/None quick actions
  - Floating filter button for easy access

### Changed

- **Enhanced UI Controls**: Added floating action buttons for better navigation
  - Filter messages button with collapsible toolbar
  - Toggle all details button for expanding/collapsing content
  - Improved back-to-top button positioning


## [0.2.7] - 2025-06-21

### Changed

- **Unwrap messages to not have double boxes**


## [0.2.6] - 2025-06-20

### Changed

- **Token usage stats and usage time intervals on top level index page + make time consistently UTC**
- **Fix example transcript link + exclude dirs from package**


## [0.2.5] - 2025-06-18

### Changed

- **Tiny Justfile fixes**
- **Create docs.yml**
- **Improve expandable details handling + open/close all button + just render short ones + add example**
- **Remove unnecessary line in error message**
- **Script release process**

## [0.2.4] - 2025-06-18

### Changed

- **More error handling**: Add better error reporting with line numbers and render fallbacks

## [0.2.3] - 2025-06-16

### Changed

- **Error handling**: Add more detailed error handling

## [0.2.2] - 2025-06-16

### Changed

- **Static Markdown**: Render Markdown in Python to make it easier to test and not require Javascipt
- **Visual Design**: Make it nicer to look at

## [0.2.1] - 2025-06-15

### Added

- **Table of Contents & Session Navigation**: Added comprehensive session navigation system
  - Interactive table of contents with session summaries and quick navigation
  - Timestamp ranges showing first-to-last timestamp for each session
  - Session-based organization with clickable navigation links
  - Floating "back to top" button for easy navigation

- **Token Usage Tracking**: Complete token consumption display and tracking
  - Individual assistant messages show token usage in headers
  - Session-level token aggregation in table of contents
  - Detailed breakdown: Input, Output, Cache Creation, Cache Read tokens
  - Data extracted from AssistantMessage.usage field in JSONL files

- **Enhanced Content Support**: Expanded message type and content handling
  - **Tool Use Rendering**: Proper display of tool invocations and results
  - **Thinking Content**: Support for Claude's internal thinking processes
  - **Image Handling**: Display of pasted images in transcript conversations
  - **Todo List Rendering**: Support for structured todo lists in messages

- **Project Hierarchy Processing**: Complete project management system
  - Process entire `~/.claude/projects/` directory by default
  - Master index page with project cards and statistics
  - Linked navigation between index and individual project pages
  - Project statistics including file counts and recent activity

- **Improved User Experience**: Enhanced interface and navigation
  - Chronological ordering of all messages across sessions
  - Session demarcation with clear visual separators
  - Always-visible scroll-to-top button
  - Space-efficient, content-dense layout design

### Changed

- **Default Behavior**: Changed default mode to process all projects instead of requiring explicit input
  - `claude-code-log` now processes `~/.claude/projects/` by default
  - Added `--all-projects` flag for explicit project processing
  - Maintained backward compatibility for single file/directory processing

- **Output Structure**: Restructured HTML output for better organization
  - Session-based navigation replaces simple chronological listing
  - Enhanced template system with comprehensive session metadata
  - Improved visual hierarchy with table of contents integration

- **Data Models**: Expanded Pydantic models for richer data representation
  - Enhanced TranscriptEntry with proper content type handling
  - Added UsageInfo model for token usage tracking
  - Improved ContentItem unions for diverse content types

### Technical

- **Template System**: Major improvements to Jinja2 template architecture
  - New session navigation template components
  - Token usage display templates
  - Enhanced message rendering with rich content support
  - Responsive design improvements

- **Testing Infrastructure**: Comprehensive test coverage expansion
  - Increased test coverage to 78%+ across all modules
  - Added visual style guide generation
  - Representative test data based on real transcript files
  - Extensive test documentation in test/README.md

- **Code Quality**: Significant refactoring and quality improvements
  - Complete Pydantic migration with proper error handling
  - Improved type hints and function documentation
  - Enhanced CLI interface with better argument parsing
  - Comprehensive linting and formatting standards

### Fixed

- **Data Processing**: Improved robustness of transcript processing
  - Better handling of malformed or incomplete JSONL entries
  - More reliable session detection and grouping
  - Enhanced error handling for edge cases in data parsing
  - Fixed HTML escaping issues in message content

- **Template Rendering**: Resolved template and rendering issues
  - Fixed session summary attachment logic
  - Improved timestamp handling and formatting
  - Better handling of mixed content types in templates
  - Resolved CSS and styling inconsistencies

## [0.1.0]

### Added

- **Summary Message Support**: Added support for `summary` type messages in JSONL transcripts
  - Summary messages are displayed with green styling and "Summary:" prefix
  - Includes special CSS class `.summary` for custom styling
  
- **System Command Visibility**: System commands (like `init`) are now shown instead of being filtered out
  - Commands appear in expandable `<details>` elements
  - Shows command name in the summary (e.g., "Command: init")
  - Full command content is revealed when expanded
  - Uses orange styling with `.system` CSS class
  
- **Markdown Rendering Support**: Automatic client-side markdown rendering
  - Uses marked.js ESM module loaded from CDN
  - Supports GitHub Flavored Markdown (GFM)
  - Renders headers, emphasis, code blocks, lists, links, and images
  - Preserves existing HTML content when present
  
- **Enhanced CSS Styling**: New styles for better visual organization
  - Added styles for `.summary` messages (green theme)
  - Added styles for `.system` messages (orange theme)  
  - Added styles for `<details>` elements with proper spacing and cursor behavior
  - Improved overall visual hierarchy

### Changed

- **System Message Filtering**: Modified system message handling logic
  - System messages with `<command-name>` tags are no longer filtered out
  - Added `extract_command_name()` function to parse command names
  - Updated `is_system_message()` function to handle command messages differently
  - Other system messages (stdout, caveats) are still filtered as before

- **Message Type Support**: Extended message type handling in `load_transcript()`
  - Now accepts `"summary"` type in addition to `"user"` and `"assistant"`
  - Updated message processing logic to handle different content structures

### Technical

- **Dependencies**: No new Python dependencies added
  - marked.js is loaded via CDN for client-side rendering
  - Maintains existing minimal dependency approach
  
- **Testing**: Added comprehensive test coverage
  - New test file `test_new_features.py` with tests for:
    - Summary message type support
    - System command message handling  
    - Markdown script inclusion
    - System message filtering behavior
  - Tests use anonymized fixtures based on real transcript data

- **Code Quality**: Improved type hints and function documentation
  - Added proper docstrings for new functions
  - Enhanced error handling for edge cases
  - Maintained backward compatibility with existing functionality

### Fixed

- **Message Processing**: Improved robustness of message content extraction
  - Better handling of mixed content types in transcript files
  - More reliable text extraction from complex message structures

## Previous Versions

Earlier versions focused on basic JSONL to HTML conversion with session demarcation and date filtering capabilities.

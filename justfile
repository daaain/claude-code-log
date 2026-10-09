# Default is to list commands
default:
    @just --list

cli *ARGS:
    uv run claude-code-log {{ ARGS }}

# Run unit + integration tests (excludes TUI, browser, and benchmark)
# `-p no:playwright`: unit runs never use the browser fixtures, so skip loading
# the pytest-playwright plugin — it imports playwright (~1s) per xdist worker on
# Windows (spawned workers re-import). See work/xdist-import-cost.md.
test:
    uv run pytest -p no:playwright -m "not (tui or browser or benchmark)" -q

# Run benchmark tests serially for stable measurements (outputs to GITHUB_STEP_SUMMARY in CI). DEBUG_TIMING enables coverage of renderer_timings.py
test-benchmark:
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark -q

# Update snapshot tests. Encodes the whole procedure from CONTRIBUTING
# ("Snapshot Testing"), because doing only part of it has twice produced a
# damaged .ambr: purge stale bytecode, regenerate SERIALLY (-n0; a parallel
# --snapshot-update once silently truncated ~6000 lines), then re-run
# read-only to prove the committed file matches what the code renders.
# A healthy result is purely additive -- "+N/-0".
update-snapshot:
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
    uv run pytest -n0 -m snapshot --snapshot-update -q
    @echo "--- verifying the regenerated snapshots read back clean ---"
    uv run pytest -n0 -m snapshot -q

# Run TUI tests (requires isolated event loop)
test-tui:
    uv run pytest -m tui -q

# Run browser tests (requires Chromium)
test-browser:
    uv run pytest -m browser -q

# Run integration tests with realistic JSONL data
test-integration:
    uv run pytest -m integration -q

# Run all tests in sequence (separated to avoid event loop conflicts)
test-all:
    #!/usr/bin/env bash
    set -e  # Exit on first failure
    echo "🧪 Running all tests in sequence..."
    echo "📦 Running unit tests..."
    uv run pytest -p no:playwright -m "not (tui or browser or integration or benchmark)" -q
    echo "🖥️  Running TUI tests..."
    uv run pytest -m tui -q
    echo "🌐 Running browser tests..."
    uv run pytest -m browser -q
    echo "🔄 Running integration tests..."
    uv run pytest -m integration -q
    echo "📊 Running benchmark tests..."
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark -q
    echo "✅ All tests completed!"

# Run tests with coverage (all categories)
test-cov:
    #!/usr/bin/env bash
    set -e  # Exit on first failure
    echo "📊 Running all tests with coverage..."
    echo "📦 Running unit tests with coverage..."
    uv run pytest -p no:playwright -m "not (tui or browser or integration or benchmark)" --cov=claude_code_log --cov-report=xml --cov-report=html --cov-report=term -q
    echo "🖥️  Running TUI tests with coverage append..."
    uv run pytest -m tui --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -q
    echo "🌐 Running browser tests with coverage append..."
    uv run pytest -m browser --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -q
    echo "🔄 Running integration tests with coverage append..."
    uv run pytest -m integration --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -q
    echo "📊 Running benchmark tests with coverage append..."
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -q
    echo "✅ All tests with coverage completed!"

format:
    uv run ruff format

lint:
    uv run ruff check --fix

typecheck:
    uv run pyright

ty:
    uv run ty check

# Lint the templates' JavaScript with oxlint (installs the optional `js`
# group). Warnings fail it. Extra arguments go to oxlint; `--keep` keeps
# the staged copy.
lint-js *ARGS:
    uv run --group js python scripts/lint_js.py {{ ARGS }}

# Format the JS, CSS, JSON and YAML with oxfmt (installs the optional `js`
# group); the template components holding Jinja go through
# scripts/fmt_templates.py. Configured in .oxfmtrc.json.
fmt-js:
    uv run --group js oxfmt
    uv run --group js python scripts/fmt_templates.py

# Check that formatting, writing nothing. Both checks run, so every file
# that needs formatting is listed.
fmt-check:
    #!/usr/bin/env bash
    status=0
    uv run --group js oxfmt --check || status=1
    uv run --group js python scripts/fmt_templates.py --check || status=1
    exit $status

# Fail-fast order: format and lint take seconds, ty is faster than pyright, the full test suite runs last
ci: format lint lint-js fmt-check ty typecheck test-all

# Regenerate the auto-generated TUI docs assets (screenshots) into docs/assets/tui
docs-gen:
    uv run python scripts/generate_tui_screenshots.py docs/assets/tui

# Serve the documentation site locally with live reload (http://127.0.0.1:8000)
# `--group docs` pulls in the docs-only deps (mkdocs et al.) on demand — the
# group is not synced by a plain `uv run`, so run it here rather than relying
# on a prior `uv sync --group docs`.
docs-serve:
    DISABLE_MKDOCS_2_WARNING=true uv run --group docs mkdocs serve

# Build the documentation site (strict: fails on broken links/nav)
docs-build:
    DISABLE_MKDOCS_2_WARNING=true uv run --group docs mkdocs build --strict

build:
    -rm dist/*
    uv build

# The NORMAL path is the tag-driven release workflow (.github/workflows/release.yml
# — trusted publishing after a maintainer approves, no token anywhere). This one
# needs PyPI credentials (e.g. UV_PUBLISH_TOKEN) on this machine.
# Publish dist/ to PyPI from a laptop — the manual escape hatch
publish:
    uv publish

# Render all test data to HTML for visual testing
render-test-data:
    #!/usr/bin/env bash
    echo "🔄 Rendering test data files..."

    # Create output directory for rendered test data
    mkdir -p test_output

    # Find all .jsonl files in test/test_data directory
    find test/test_data -name "*.jsonl" -type f | while read -r jsonl_file; do
        filename=$(basename "$jsonl_file" .jsonl)
        echo "  📄 Rendering: $filename.jsonl"

        # Generate HTML output
        uv run claude-code-log "$jsonl_file" -o "test_output/${filename}.html"

        if [ $? -eq 0 ]; then
            echo "  ✅ Created: test_output/${filename}.html"
        else
            echo "  ❌ Failed to render: $filename.jsonl"
        fi
    done

    echo "🎉 Test data rendering complete! Check test_output/ directory"
    echo "💡 Open HTML files in browser to review output"

style-guide:
    uv run python scripts/generate_style_guide.py

# Everything here is local: `git tag -d X.Y.Z && git reset --hard HEAD~1` undoes it.
# Bump, changelog, commit and tag a release - e.g. `just release-prep 0.2.5` or `just release-prep minor`
release-prep version_or_bump:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "🚀 Starting release process"

    # The tag has to name a commit on main: one cut on a branch and then
    # squash-merged would point at a commit main never has.
    if [[ "$(git branch --show-current)" != main ]]; then
        echo "❌ Error: Release from main (on '$(git branch --show-current)')"
        exit 1
    fi

    if [[ -n $(git status --porcelain) ]]; then
        echo "❌ Error: There are uncommitted changes. Please commit or stash them first."
        git status --short
        exit 1
    fi

    echo "✅ Git working directory is clean"

    # Determine the new version
    if [[ "{{ version_or_bump }}" =~ ^(major|minor|patch)$ ]]; then
        # Get current version from pyproject.toml
        CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
        echo "📌 Current version: $CURRENT_VERSION"

        # Parse version components
        IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

        # Increment based on bump type
        case "{{ version_or_bump }}" in
            major)
                NEW_VERSION="$((MAJOR + 1)).0.0"
                ;;
            minor)
                NEW_VERSION="$MAJOR.$((MINOR + 1)).0"
                ;;
            patch)
                NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
                ;;
        esac

        echo "📈 Bumping {{ version_or_bump }} version to: $NEW_VERSION"
        VERSION=$NEW_VERSION
    else
        # Direct version was provided
        VERSION="{{ version_or_bump }}"
        echo "📌 Using provided version: $VERSION"
    fi

    echo "📝 Updating version in pyproject.toml to $VERSION"
    tmp=$(mktemp)
    sed "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml > "$tmp" && mv "$tmp" pyproject.toml

    echo "🔄 Running uv sync to update lock file"
    uv sync

    LAST_TAG=$(git tag --sort=-version:refname | head -n 1 || echo "")
    echo "📋 Generating changelog from tag $LAST_TAG to HEAD"
    COMMIT_RANGE="$LAST_TAG..HEAD"

    echo "📝 Updating CHANGELOG.md"
    TEMP_CHANGELOG=$(mktemp)
    NEW_ENTRY=$(mktemp)

    # Create the new changelog entry
    {
        echo "## [${VERSION}] - $(date +%Y-%m-%d)"
        echo ""
        echo "### Changed"
        echo ""

        # Add commit messages since last tag
        if [[ -n "$COMMIT_RANGE" ]]; then
            git log --pretty=format:"- %s" "$COMMIT_RANGE" | sed 's/^- /- **/' | sed 's/$/**/' || true
        else
            git log --pretty=format:"- %s" | sed 's/^- /- **/' | sed 's/$/**/' || true
        fi
        echo ""
    } > "$NEW_ENTRY"

    # Insert new entry after the header (after line 7)
    {
        head -n 7 CHANGELOG.md
        echo ""
        cat "$NEW_ENTRY"
        echo ""
        tail -n +8 CHANGELOG.md
    } > "$TEMP_CHANGELOG"

    mv "$TEMP_CHANGELOG" CHANGELOG.md
    rm "$NEW_ENTRY"

    echo "💾 Committing version bump and changelog"
    git add pyproject.toml uv.lock CHANGELOG.md
    git commit -m "Release $VERSION"

    echo "🏷️  Creating tag $VERSION"
    git tag "$VERSION" -m "Release $VERSION"

    echo "🎉 Release $VERSION created successfully!"
    echo "🔍 Check it with 'just release-preview' and 'git show --stat HEAD'"
    echo "📦 Then run 'just release-push' to start the release workflow"

# Nothing is published until a maintainer approves the `pypi` deployment in the
# run — and once it is, PyPI never lets that version number be reused.
# Push the release commit and tag, starting the release workflow (.github/workflows/release.yml)
release-push:
    #!/usr/bin/env bash
    set -euo pipefail

    # It pushes the local main, so the release commit has to be on it.
    if [[ "$(git branch --show-current)" != main ]]; then
        echo "❌ Error: Release from main (on '$(git branch --show-current)')"
        exit 1
    fi

    TAG=$(git tag --sort=-version:refname | head -n 1)
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "$TAG^{commit}")" ]]; then
        echo "❌ Error: HEAD is not the latest tag ($TAG) — run 'just release-prep' first"
        exit 1
    fi

    # --atomic: both or neither — a tag pushed without its commit would name
    # one main doesn't have.
    echo "⬆️  Pushing main and tag $TAG to origin"
    git push --atomic origin main "$TAG"

    echo "⏳ The release workflow waits for CI on the release commit, then for a maintainer"
    echo "   to approve the PyPI deployment; the GitHub Release follows PyPI:"
    echo "🔗 https://github.com/daaain/claude-code-log/actions/workflows/release.yml"

# Helper command to preview what would be in the GitHub release
release-preview version="":
    #!/usr/bin/env bash
    set -euo pipefail

    # Determine which version to preview
    if [[ -n "{{ version }}" ]]; then
        TARGET_TAG="{{ version }}"
        echo "📋 Preview of release notes for specified version: $TARGET_TAG"
    else
        TARGET_TAG=$(git tag --sort=-version:refname | head -n 1 || echo "")
        if [[ -z "$TARGET_TAG" ]]; then
            echo "⚠️  No tags found. Showing what would be created for next release..."
            echo ""
            echo "### Changed"
            echo ""
            git log --pretty=format:"- %s" -10
            exit 0
        fi
        echo "📋 Preview of release notes for latest tag: $TARGET_TAG"
    fi

    # Verify the tag exists (if specified)
    if [[ -n "{{ version }}" ]] && ! git rev-parse "$TARGET_TAG" >/dev/null 2>&1; then
        echo "❌ Error: Tag $TARGET_TAG does not exist"
        exit 1
    fi

    echo ""
    uv run --no-project python scripts/release_notes.py "$TARGET_TAG"

# Render the showcase example transcript from bundled sample data into
# test_output/ for local preview. The docs build publishes its own copy to the
# site via docs/gen_pages.py, so this is only for quick standalone inspection.
example:
    uv run python scripts/generate_example_output.py

backup:
    rsync -a ~/.claude/projects ~/.claude-backup/projects

clear-cache:
    just cli --clear-cache
    just cli --clear-html

regen-all: backup render-test-data style-guide cli example

# Re-record the demo videos (scripts/demos/record.py) into .demos/;
# e.g. `just demos --only branches`, or `just demos --publish` to update the docs
demos *ARGS:
    uv run --group demos python scripts/demos/record.py {{ARGS}}

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
    uv run pytest -p no:playwright -m "not (tui or browser or benchmark)" -v

# Run benchmark tests serially for stable measurements (outputs to GITHUB_STEP_SUMMARY in CI). DEBUG_TIMING enables coverage of renderer_timings.py
test-benchmark:
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark -v

# Update snapshot tests (runs serially for deterministic file ordering)
update-snapshot:
    uv run pytest -n0 -m snapshot --snapshot-update -v

# Run TUI tests (requires isolated event loop)
test-tui:
    uv run pytest -m tui -v

# Run browser tests (requires Chromium)
test-browser:
    uv run pytest -m browser -v

# Run integration tests with realistic JSONL data
test-integration:
    uv run pytest -m integration -v

# Run all tests in sequence (separated to avoid event loop conflicts)
test-all:
    #!/usr/bin/env bash
    set -e  # Exit on first failure
    echo "🧪 Running all tests in sequence..."
    echo "📦 Running unit tests..."
    uv run pytest -p no:playwright -m "not (tui or browser or integration or benchmark)" -v
    echo "🖥️  Running TUI tests..."
    uv run pytest -m tui -v
    echo "🌐 Running browser tests..."
    uv run pytest -m browser -v
    echo "🔄 Running integration tests..."
    uv run pytest -m integration -v
    echo "📊 Running benchmark tests..."
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark -v
    echo "✅ All tests completed!"

# Run tests with coverage (all categories)
test-cov:
    #!/usr/bin/env bash
    set -e  # Exit on first failure
    echo "📊 Running all tests with coverage..."
    echo "📦 Running unit tests with coverage..."
    uv run pytest -p no:playwright -m "not (tui or browser or integration or benchmark)" --cov=claude_code_log --cov-report=xml --cov-report=html --cov-report=term -v
    echo "🖥️  Running TUI tests with coverage append..."
    uv run pytest -m tui --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -v
    echo "🌐 Running browser tests with coverage append..."
    uv run pytest -m browser --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -v
    echo "🔄 Running integration tests with coverage append..."
    uv run pytest -m integration --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -v
    echo "📊 Running benchmark tests with coverage append..."
    CLAUDE_CODE_LOG_DEBUG_TIMING=1 uv run pytest -n0 -m benchmark --cov=claude_code_log --cov-append --cov-report=xml --cov-report=html --cov-report=term -v
    echo "✅ All tests with coverage completed!"

format:
    uv run ruff format

lint:
    uv run ruff check --fix

typecheck:
    uv run pyright

ty:
    uv run ty check

ci: format test-all lint typecheck ty

# Regenerate the auto-generated TUI docs assets (screenshots) into docs/assets/tui
docs-gen:
    uv run python scripts/generate_tui_screenshots.py docs/assets/tui

# Serve the documentation site locally with live reload (http://127.0.0.1:8000)
docs-serve:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

# Build the documentation site (strict: fails on broken links/nav)
docs-build:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict

build:
    -rm dist/*
    uv build

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

# Release a new version - e.g. `just release-prep 0.2.5` or `just release-prep minor`
release-prep version_or_bump:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "🚀 Starting release process"

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
    echo "📦 You can now run 'just release-push' to publish to PyPI and GitHub"

release-push:
    #!/usr/bin/env bash
    set -euo pipefail

    LAST_TAG=$(git tag --sort=-version:refname | head -n 1 || echo "")

    echo "📦 Build and publish package $LAST_TAG"
    just build
    just publish

    echo "⬆️  Pushing commit to origin"
    git push origin main

    echo "🏷️  Pushing tag $LAST_TAG"
    git push origin $LAST_TAG

    echo "🚀 Creating GitHub release"
    just github-release

# Create a GitHub release from the latest tag or a specific version
github-release version="":
    #!/usr/bin/env bash
    set -euo pipefail

    # Determine which tag to use
    if [[ -n "{{ version }}" ]]; then
        TARGET_TAG="{{ version }}"
        echo "📦 Creating GitHub release for specified version: $TARGET_TAG"
    else
        TARGET_TAG=$(git tag --sort=-version:refname | head -n 1)
        if [[ -z "$TARGET_TAG" ]]; then
            echo "❌ Error: No tags found"
            exit 1
        fi
        echo "📦 Creating GitHub release for latest tag: $TARGET_TAG"
    fi

    # Verify the tag exists
    if ! git rev-parse "$TARGET_TAG" >/dev/null 2>&1; then
        echo "❌ Error: Tag $TARGET_TAG does not exist"
        exit 1
    fi

    # Get all tags sorted by version for finding the previous tag
    ALL_TAGS=$(git tag --sort=-version:refname)

    # Find the previous tag relative to TARGET_TAG
    PREVIOUS_TAG=""
    FOUND_TARGET=false
    while IFS= read -r tag; do
        if [[ "$FOUND_TARGET" == true ]]; then
            PREVIOUS_TAG="$tag"
            break
        fi
        if [[ "$tag" == "$TARGET_TAG" ]]; then
            FOUND_TARGET=true
        fi
    done <<< "$ALL_TAGS"

    echo "📝 Extracting release notes for $TARGET_TAG from CHANGELOG.md"

    # Extract the release notes for this version from CHANGELOG.md
    # This looks for the section starting with ## [$TARGET_TAG] and extracts until the next ## or end of file
    RELEASE_NOTES_FILE=$(mktemp)
    awk -v tag="$TARGET_TAG" '
        /^## \[/ { 
            if (found && started) exit; 
            if (index($0, "[" tag "]") > 0) {
                found=1;
                next;
            }
        }
        found && !started && /^$/ { started=1; next }
        found && started && /^## \[/ { exit }
        found && started { print }
    ' CHANGELOG.md > "$RELEASE_NOTES_FILE"

    # Check if we found any release notes
    if [[ ! -s "$RELEASE_NOTES_FILE" ]]; then
        echo "⚠️  Warning: No release notes found for $TARGET_TAG in CHANGELOG.md"
        echo "Creating release with minimal notes..."
        echo "Release $TARGET_TAG" > "$RELEASE_NOTES_FILE"
    fi

    # Add a link to the full changelog if we have a previous tag
    if [[ -n "$PREVIOUS_TAG" ]]; then
        echo "" >> "$RELEASE_NOTES_FILE"
        echo "**Full Changelog**: https://github.com/daaain/claude-code-log/compare/$PREVIOUS_TAG...$TARGET_TAG" >> "$RELEASE_NOTES_FILE"
    fi

    # Check if the release already exists
    if gh release view "$TARGET_TAG" &>/dev/null; then
        echo "⚠️  Release $TARGET_TAG already exists. Updating it..."
        gh release edit "$TARGET_TAG" \
            --title "Release $TARGET_TAG" \
            --notes-file "$RELEASE_NOTES_FILE"
    else
        echo "🎉 Creating new GitHub release for $TARGET_TAG"
        gh release create "$TARGET_TAG" \
            --title "Release $TARGET_TAG" \
            --notes-file "$RELEASE_NOTES_FILE"
    fi

    # Check if we have built artifacts to upload (only for current/latest releases)
    if [[ -z "{{ version }}" ]] || [[ "$TARGET_TAG" == $(git tag --sort=-version:refname | head -n 1) ]]; then
        if [[ -f "dist/claude_code_log-${TARGET_TAG#v}.tar.gz" ]]; then
            echo "📦 Uploading source distribution"
            gh release upload "$TARGET_TAG" "dist/claude_code_log-${TARGET_TAG#v}.tar.gz" --clobber
        fi

        if [[ -f "dist/claude_code_log-${TARGET_TAG#v}-py3-none-any.whl" ]]; then
            echo "📦 Uploading wheel distribution"
            gh release upload "$TARGET_TAG" "dist/claude_code_log-${TARGET_TAG#v}-py3-none-any.whl" --clobber
        fi
        # The example transcript showcase is published to the docs site
        # (see docs/gen_pages.py), not attached to releases.
    fi

    rm "$RELEASE_NOTES_FILE"
    echo "✅ GitHub release created/updated successfully!"
    echo "🔗 View it at: https://github.com/daaain/claude-code-log/releases/tag/$TARGET_TAG"

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
    awk -v tag="$TARGET_TAG" '
        /^## \[/ { 
            if (found && started) exit; 
            if (index($0, "[" tag "]") > 0) {
                found=1;
                next;
            }
        }
        found && !started && /^$/ { started=1; next }
        found && started && /^## \[/ { exit }
        found && started { print }
    ' CHANGELOG.md

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

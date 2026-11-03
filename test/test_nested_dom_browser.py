"""Live-browser tests for the dissociated nested-DOM (issue #174) and the
CodeRabbit follow-ups on PR #191:

- A ``.filtered-hidden`` parent card hides only its OWN row, never its
  ``.children`` (which are siblings, not descendants). This is what makes
  ``updateVisibleCounts()``'s ``:not(.filtered-hidden)`` count correct: a
  child of a filtered-out parent is genuinely visible AND counted, with no
  cascade mismatch (CR comment 1b).
- Folding a node hides the fork-point marker too, because the template
  renders it INSIDE ``.children`` (CR comment 2 — fork-point fold).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

TEAMMATES = (
    Path(__file__).parent
    / "test_data"
    / "teammates"
    / "ef000000-0000-4000-8000-000000000001.jsonl"
)


def _render(tmp_path: Path) -> str:
    """Render the teammates fixture (deeply nested: user → assistant/system
    children) to an HTML file and return a ``file://`` URL."""
    html = generate_html(load_transcript(TEAMMATES))
    out = tmp_path / "nested.html"
    out.write_text(html, encoding="utf-8")
    return f"file://{out}"


class TestFilteredParentDoesNotCascade:
    """CR 1b: a filtered-out parent must not hide — nor cause overcounting
    of — its children, which live in a sibling ``.children`` container."""

    @pytest.mark.browser
    def test_filtered_parent_keeps_children_visible_and_counted(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path))
        page.wait_for_timeout(300)

        result = page.evaluate(
            """() => {
                // Find a card that has a .children sibling holding >=1 other card.
                const cards = Array.from(document.querySelectorAll('.message[id^="msg-"]'));
                let parent = null, childCard = null;
                for (const c of cards) {
                    const cc = c.parentElement &&
                        c.parentElement.querySelector(':scope > .children');
                    const inner = cc && cc.querySelector('.message[id^="msg-"]');
                    if (inner) { parent = c; childCard = inner; break; }
                }
                if (!parent) return { error: 'no parent-with-children found' };

                // Simulate the filter hiding the parent card (applyFilter adds
                // .filtered-hidden to the .message card by its type).
                parent.classList.add('filtered-hidden');

                // The count selector used by updateVisibleCounts():
                const countedAsVisible = document
                    .querySelector(`.message[id="${childCard.id}"]:not(.filtered-hidden)`) !== null;

                return {
                    parentDisplay: getComputedStyle(parent).display,         // none
                    childDisplay: getComputedStyle(childCard).display,        // not none
                    childCountedAsVisible: countedAsVisible,                  // true
                    childActuallyVisible: getComputedStyle(childCard).display !== 'none',
                };
            }"""
        )

        assert "error" not in result, result
        # Parent's own row is hidden ...
        assert result["parentDisplay"] == "none"
        # ... but its child (a sibling under .children) stays visible ...
        assert result["childActuallyVisible"] is True
        # ... and the count selector's "counted visible" matches reality
        # (no cascade overcount — CR 1b).
        assert result["childCountedAsVisible"] == result["childActuallyVisible"]


class TestFoldHidesForkPoint:
    """CR 2: a fork-point rendered inside ``.children`` folds away with the
    subtree instead of orphaning."""

    @pytest.mark.browser
    def test_folding_hides_fork_point_inside_children(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path))
        page.wait_for_timeout(300)

        # Real within-session fork nodes have no children (the branches are
        # separate top-level nodes), so no fixture yields a foldable node that
        # also owns a fork-point. Synthesize that co-occurrence by injecting a
        # fork-point into a foldable node's .children — exactly where the
        # template places it — then fold and assert it hides.
        result = page.evaluate(
            """() => {
                // Find a node that is currently EXPANDED (its .children is
                // visible and its fold-one section is not folded), so a single
                // click deterministically folds it.
                const sections = Array.from(
                    document.querySelectorAll('.fold-bar-section.fold-one-level'));
                for (const sec of sections) {
                    if (sec.classList.contains('folded')) continue;
                    const id = sec.getAttribute('data-target');
                    const card = document.getElementById(`msg-${id}`);
                    const cc = card && card.parentElement.querySelector(':scope > .children');
                    if (!cc || getComputedStyle(cc).display === 'none') continue;

                    // Inject a fork-point INSIDE .children (mirrors the template,
                    // which renders the marker there so it folds with the subtree).
                    const fp = document.createElement('div');
                    fp.className = 'fork-point';
                    fp.id = 'injected-fork-point';
                    cc.appendChild(fp);

                    // checkVisibility() accounts for ANCESTOR display:none
                    // (getComputedStyle(fp).display would still report the
                    // element's own 'block' even inside a hidden container).
                    const before = fp.checkVisibility();          // true
                    sec.click();                                  // fold the subtree
                    const after = fp.checkVisibility();           // false
                    return { before, after, ccAfter: getComputedStyle(cc).display };
                }
                return { error: 'no expanded foldable node found' };
            }"""
        )

        assert "error" not in result, result
        assert result["before"] is True
        # Folding the node hid its .children, taking the fork-point with it.
        assert result["ccAfter"] == "none"
        assert result["after"] is False

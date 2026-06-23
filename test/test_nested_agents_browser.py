"""Runtime CSS contract for nested sub-agent groups (#213).

Every spawn boundary indents its transcript group and frames it with a
line whose colour cycles by agent-nesting depth (the #213 visual layer:
depth 1 = tool-green, depth 2 = blue, … via a 5-colour ramp). Depth
accumulates through DOM nesting (a depth-2 group lives inside a depth-1
group). Computed styles are read directly so default fold state doesn't
matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import _integrate_agent_entries, load_transcript
from claude_code_log.html.renderer import generate_html

TRUNK_SID = "33330000-0000-4000-8000-000000000001"
TRUNK = Path(__file__).parent / "test_data" / "nested_agents" / f"{TRUNK_SID}.jsonl"

# Ring colours (global_styles.css --agent-ring-N) as computed rgb().
RING_RGB = {
    1: "rgb(76, 175, 80)",  # #4caf50 tool-green
    2: "rgb(30, 136, 229)",  # #1e88e5 blue
    3: "rgb(142, 68, 173)",  # #8e44ad purple
    4: "rgb(230, 126, 34)",  # #e67e22 orange
    5: "rgb(0, 137, 123)",  # #00897b teal
}

GROUPS_JS = """() => {
    const isGroup = el => el.classList && el.classList.contains('children')
        && el.querySelector(':scope > .message-node > .message.sidechain');
    const groups = Array.from(document.querySelectorAll('.children')).filter(isGroup);
    return groups.map(g => {
        const cs = getComputedStyle(g);
        // Agent depth of the cards this group frames (from agent-depth-N).
        const inner = g.querySelector(':scope > .message-node > .message.sidechain');
        const m = (inner.className.match(/agent-depth-(\\d+)/) || [])[1];
        let nesting = 0, el = g.parentElement;
        while (el) {
            if (isGroup(el)) nesting += 1;
            el = el.parentElement;
        }
        return {
            marginLeft: cs.marginLeft,
            borderWidth: cs.borderLeftWidth,
            borderColor: cs.borderLeftColor,
            innerDepth: m ? parseInt(m, 10) : null,
            enclosingGroups: nesting,
        };
    });
}"""


class TestNestedAgentGroupCss:
    @pytest.mark.browser
    def test_group_line_colour_cycles_by_depth(
        self, page: Page, tmp_path: Path
    ) -> None:
        entries = load_transcript(TRUNK, silent=True)
        _integrate_agent_entries(entries)
        html_path = tmp_path / "nested.html"
        html_path.write_text(generate_html(entries, "Nested CSS"), encoding="utf-8")
        page.set_viewport_size({"width": 1600, "height": 1200})
        page.goto(f"file://{html_path}")

        groups = page.evaluate(GROUPS_JS)
        # 2×2: mid1 + mid2 (d1) with leaf22's surviving group inside mid2;
        # chain: chain1 (d1) with chain2's group inside; interrupted (d1).
        assert len(groups) == 6
        by_depth: dict[int, int] = {}
        for g in groups:
            assert g["borderWidth"] == "2px", g
            # Shallow levels keep the comfortable 2em step.
            assert g["marginLeft"] == "32px", g
            assert g["innerDepth"] in (1, 2), g
            ring = ((g["innerDepth"] - 1) % 5) + 1
            assert g["borderColor"] == RING_RGB[ring], g
            by_depth[g["innerDepth"]] = by_depth.get(g["innerDepth"], 0) + 1

        # Four depth-1 groups (green), two depth-2 groups (blue).
        assert by_depth == {1: 4, 2: 2}, by_depth

        # Depth accumulates structurally: the two depth-2 groups are nested
        # inside a depth-1 group's subtree.
        nested = [g for g in groups if g["enclosingGroups"] > 0]
        assert len(nested) == 2, groups

    @pytest.mark.browser
    def test_depth_badge_and_collapsed_marker_present(
        self, page: Page, tmp_path: Path
    ) -> None:
        entries = load_transcript(TRUNK, silent=True)
        _integrate_agent_entries(entries)
        html_path = tmp_path / "nested.html"
        html_path.write_text(generate_html(entries, "Nested CSS"), encoding="utf-8")
        page.set_viewport_size({"width": 1600, "height": 1200})
        page.goto(f"file://{html_path}")

        # Depth badges appear only on spawns that open depth >= 2; top-level
        # spawns (→ depth 1) carry none.
        badges = page.evaluate(
            "() => Array.from(document.querySelectorAll('.agent-depth-badge'))"
            ".map(b => b.textContent)"
        )
        assert sorted(badges) == [
            "Depth 2",
            "Depth 2",
            "Depth 2",
            "Depth 2",
            "Depth 2",
            "Depth 3",
        ], badges

        # A badge's pill colour matches the ring of the depth it opens.
        d3_colour = page.evaluate(
            "() => { const b = Array.from(document.querySelectorAll("
            "'.agent-depth-badge')).find(x => x.textContent === 'Depth 3');"
            " return getComputedStyle(b).backgroundColor; }"
        )
        assert d3_colour == RING_RGB[3], d3_colour  # depth 3 → ring 3 purple

        # Fully-collapsed nested spawns (leaf11/12/21 + chain3) carry the
        # "≡ full transcript" marker; leaf22 (divergent result) does not.
        markers = page.evaluate(
            "() => document.querySelectorAll('.spawn-collapsed-marker').length"
        )
        assert markers == 4, markers

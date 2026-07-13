"""Live-browser tests for the spliced dynamic-workflow run tree (#174 PR3).

The phase/agent cards are synthesized nodes attached via ``.children`` on the
same nested DOM the rest of the transcript uses (PR0 / #191), so the existing
fold machine must drive them: folding a Workflow *phase* hides its agent
children (and their grafted side-channel transcripts) exactly like any other
foldable node — provided the synthetic nodes carry the fold-state fields the
splice sets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.html.renderer import generate_html

WORKFLOW_DIR = Path(__file__).parent / "test_data" / "workflow_basic"


def _render(tmp_path: Path) -> str:
    """Render the workflow_basic directory (Workflow tool_use with a spliced
    phase/agent sub-tree) to an HTML file and return a ``file://`` URL."""
    msgs, tree = load_directory_transcripts(WORKFLOW_DIR, silent=True)
    html = generate_html(msgs, session_tree=tree)
    out = tmp_path / "workflow.html"
    out.write_text(html, encoding="utf-8")
    return f"file://{out}"


class TestWorkflowPhaseFold:
    """A spliced ``workflow_phase`` card drives its ``workflow_agent`` children
    through the shared nested-DOM fold machine: clicking its fold control
    toggles the agents' visibility, and a second click restores it.

    (Deeply-nested subtrees start folded by default, so this asserts a *toggle*
    rather than a fixed initial state — proving the fold machine operates on the
    synthetic nodes, which requires the fold-state fields the splice sets.)"""

    @pytest.mark.browser
    def test_phase_fold_control_toggles_agents(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path))
        page.wait_for_timeout(300)

        result = page.evaluate(
            """() => {
                // A workflow_phase card whose sibling .children holds an agent.
                // (Non-session/non-user nodes start folded, so we assert the
                // fold control TOGGLES the phase's own children container —
                // independent of any ancestor's fold state, i.e. offsetParent.)
                const phases = Array.from(
                    document.querySelectorAll('.message.workflow_phase[id^="msg-"]'));
                for (const phase of phases) {
                    const cc = phase.parentElement &&
                        phase.parentElement.querySelector(':scope > .children');
                    const agent = cc && cc.querySelector('.message.workflow_agent');
                    const bar = phase.querySelector(
                        ':scope > .fold-bar .fold-bar-section.fold-one-level');
                    if (!agent || !bar || !cc) continue;

                    const hidden = () => cc.style.display === 'none';
                    const d0 = hidden();
                    bar.click();
                    const d1 = hidden();
                    bar.click();
                    const d2 = hidden();
                    return { found: true, phaseId: phase.id, d0, d1, d2 };
                }
                return { found: false };
            }"""
        )

        assert result.get("found"), "no workflow_phase with agents + fold bar found"
        # One click flips the children container's hidden state; a second
        # restores it — the fold machine operates on the synthetic phase node.
        assert result["d1"] != result["d0"]
        assert result["d2"] == result["d0"]


class TestWorkflowRuntimeCss:
    """Runtime contract for the workflow group borders (CR on the polish PR):
    snapshots embed the CSS *text*, so a DOM-structure change that silently
    breaks the ``:has()`` selectors would not fail them — only computed-style
    assertions pin the selector↔DOM contract."""

    @pytest.mark.browser
    def test_group_borders_and_alignment(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path))
        page.wait_for_timeout(300)

        result = page.evaluate(
            """() => {
                const phase = document.querySelector('.message.workflow_phase');
                if (!phase) return { found: false };
                const phaseNode = phase.parentElement;            // .message-node
                const phasesGroup = phaseNode.parentElement;      // .children (of the pair)
                const agentsGroup = phaseNode.querySelector(':scope > .children');
                const agentNode = agentsGroup &&
                    agentsGroup.querySelector(':scope > .message-node');
                const agent = agentNode &&
                    agentNode.querySelector(':scope > .message.workflow_agent');
                const scGroup = agentNode &&
                    agentNode.querySelector(':scope > .children');
                if (!agent || !scGroup) return { found: false };
                // Reveal folded containers so geometry is honest.
                for (const c of [phasesGroup, agentsGroup, scGroup]) {
                    c.style.display = '';
                }
                const cs = (el) => {
                    const s = getComputedStyle(el);
                    return { bw: s.borderLeftWidth, bc: s.borderLeftColor };
                };
                const x = (el) => el.getBoundingClientRect().left;
                return {
                    found: true,
                    phasesGroup: cs(phasesGroup),     // suppressed: 0px
                    agentsGroup: cs(agentsGroup),     // dark green, 2px
                    scGroup: cs(scGroup),             // grey, 2px
                    phaseAligned: Math.abs(x(phase) - x(agentsGroup)) < 1,
                    agentAligned: Math.abs(x(agent) - x(scGroup)) < 1,
                };
            }"""
        )

        assert result.get("found"), "workflow phase/agent structure not found"
        # Workflow-level group line is suppressed; phase + agent lines drawn.
        assert result["phasesGroup"]["bw"] == "0px"
        assert result["agentsGroup"]["bw"] == "2px"
        assert result["agentsGroup"]["bc"] == "rgb(58, 125, 60)"  # #3a7d3c
        assert result["scGroup"]["bw"] == "2px"
        assert result["scGroup"]["bc"] == "rgb(158, 158, 158)"  # #9e9e9e
        # Each group border continues its parent card's border (same x).
        assert result["phaseAligned"] is True
        assert result["agentAligned"] is True


class TestPhasePillNavigation:
    """Clicking a phase pill in the Workflow header navigates to the phase
    card: the hash updates and the ``hashchange`` handler unfolds the folded
    ancestors so the target becomes visible (CR on the polish PR)."""

    @pytest.mark.browser
    def test_pill_click_navigates_and_unfolds(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path))
        page.wait_for_timeout(300)

        target_id = page.evaluate(
            """() => {
                const pill = document.querySelector('a.workflow-phase-pill');
                return pill ? pill.getAttribute('href').slice(1) : null;
            }"""
        )
        assert target_id, "no linked phase pill found"

        hidden_before = page.evaluate(
            f"() => document.getElementById('{target_id}').offsetParent === null"
        )
        assert hidden_before, "phase card should start folded away"

        # The Workflow card itself starts inside folded ancestors — reveal it
        # the way a user arriving from the session nav would: jump to its
        # anchor and let the built-in hashchange unfold expose it.
        page.evaluate(
            """() => {
                const card = document.querySelector(
                    '.message.tool_use:has(.workflow-meta)');
                window.location.hash = '#' + card.id;
            }"""
        )
        page.wait_for_function(
            "() => document.querySelector('a.workflow-phase-pill')"
            ".offsetParent !== null"
        )

        page.click("a.workflow-phase-pill")
        # The hashchange handler runs asynchronously — poll, don't assume.
        page.wait_for_function(f"() => window.location.hash === '#{target_id}'")
        page.wait_for_function(
            f"() => document.getElementById('{target_id}').offsetParent !== null"
        )

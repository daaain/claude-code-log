"""Playwright browser tests for the auto-memory filter toggle (issue #192).

Memory interactions (Read/Write/Edit on a memory/ path) carry a ``memory``
CSS modifier on top of their tool_use/tool_result classes. The ``memory``
filter toggle behaves as a cross-cutting modifier — like ``sidechain`` — so it
can both *hide* memory noise from the tool stream and *isolate* memory-only.
"""

import shutil
import tempfile
from pathlib import Path
from typing import List

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

# File-relative so the tests don't depend on the pytest working directory.
_DATA = Path(__file__).parent / "test_data"
MEMORY_FIXTURE = _DATA / "memory_interactions.jsonl"
MEMORY_SIDECHAIN_FIXTURE = _DATA / "memory_sidechain.jsonl"


class TestMemoryFilterBrowser:
    def setup_method(self):
        self.temp_files: List[Path] = []
        # Private folder per test: pages and their assets/ sidecar land
        # here, not in the shared system temp dir.
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        for f in self.temp_files:
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _html(self, title: str, fixture: Path = MEMORY_FIXTURE) -> Path:
        messages = load_transcript(fixture)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            delete=False,
            encoding="utf-8",
            dir=self.temp_dir,
        ) as f:
            temp_file = Path(f.name)
            f.write(generate_html(messages, title, output_dir=temp_file.parent))
        self.temp_files.append(temp_file)
        return temp_file

    @pytest.mark.browser
    def test_memory_toggle_present_with_count(self, page: Page):
        """The 🧠 Memory toggle exists and counts the 4 memory messages
        (2 tool_use calls + 2 tool_result)."""
        temp_file = self._html("Memory Toggle Present")
        page.goto(f"file://{temp_file}?filter=user,assistant")  # show toolbar

        memory_toggle = page.locator('[data-type="memory"]')
        expect(memory_toggle).to_have_count(1)
        expect(memory_toggle).to_be_visible()
        # Count badge shows total memory messages: 2 tool_use + 2 tool_result.
        # Format is "(N)" when all-active, "(visible/total)" when filtered;
        # both end in "4)" here since this view has memory inactive.
        expect(memory_toggle).to_contain_text("4)")

    @pytest.mark.browser
    def test_memory_isolation(self, page: Page):
        """filter=memory isolates memory interactions: memory messages stay
        visible, the non-memory source Read is hidden."""
        temp_file = self._html("Memory Isolation")
        page.goto(f"file://{temp_file}?filter=memory")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            ".message.tool_use:not(.memory).filtered-hidden",
            state="attached",
            timeout=5000,
        )

        visible_memory = page.locator(".message.memory:not(.filtered-hidden)")
        assert visible_memory.count() > 0, "Memory messages should be visible"

        # The non-memory source-file Read must be hidden.
        visible_non_memory_tool = page.locator(
            ".message.tool_use:not(.memory):not(.filtered-hidden)"
        )
        assert visible_non_memory_tool.count() == 0, (
            "Non-memory tool messages should be hidden when isolating memory"
        )

    @pytest.mark.browser
    def test_memory_hidden_while_tools_shown(self, page: Page):
        """filter=tool (memory toggle off) hides memory interactions but keeps
        the non-memory tool stream visible."""
        temp_file = self._html("Memory Hidden")
        page.goto(f"file://{temp_file}?filter=tool")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(
            ".message.memory.filtered-hidden", state="attached", timeout=5000
        )

        # Memory messages hidden.
        visible_memory = page.locator(".message.memory:not(.filtered-hidden)")
        assert visible_memory.count() == 0, (
            "Memory messages should be hidden when the memory toggle is off"
        )

        # Non-memory tool messages still visible.
        visible_non_memory_tool = page.locator(
            ".message.tool_use:not(.memory):not(.filtered-hidden)"
        )
        assert visible_non_memory_tool.count() > 0, (
            "Non-memory tool messages should remain visible"
        )

    @pytest.mark.browser
    def test_memory_timeline_lane(self, page: Page):
        """Memory interactions get their own 🧠 Memory lane in the timeline,
        kept in lockstep with the filter toggle."""
        temp_file = self._html("Memory Timeline Lane")
        page.goto(f"file://{temp_file}")

        # Activate the timeline (loads vis-timeline from CDN).
        page.locator("#toggleTimeline").click()
        page.wait_for_selector("#timeline-container", state="attached")
        page.wait_for_selector(".vis-timeline", timeout=30000)
        page.wait_for_selector(".vis-item", timeout=5000)

        # Wait on the actual condition (the lane label rendering) rather than a
        # fixed sleep, so this can't flake on a slow group render.
        memory_lane = page.locator('.vis-label:has-text("🧠 Memory")')
        memory_lane.first.wait_for(state="attached", timeout=10000)
        assert memory_lane.count() > 0, "Timeline should have a 🧠 Memory lane"

    @pytest.mark.browser
    def test_memory_in_sidechain_stays_filterable(self, page: Page):
        """Regression (CR #204): a memory interaction inside a sidechain must
        stay governed by the memory toggle, not get clobbered by sidechain.
        Isolating memory (filter=memory) keeps the sidechain memory Read
        visible while hiding the plain sidechain assistant message."""
        temp_file = self._html("Memory In Sidechain", MEMORY_SIDECHAIN_FIXTURE)
        page.goto(f"file://{temp_file}?filter=memory")
        page.wait_for_load_state("networkidle")

        # The memory Read lives inside a sidechain (classes: tool_use memory
        # sidechain). With only memory active it must be VISIBLE.
        sidechain_memory = page.locator(
            ".message.memory.sidechain:not(.filtered-hidden)"
        )
        assert sidechain_memory.count() > 0, (
            "Memory interaction inside a sidechain should stay visible when "
            "isolating memory"
        )

        # A plain (non-memory) sidechain message must be hidden under filter=memory.
        plain_sidechain = page.locator(
            ".message.sidechain:not(.memory):not(.filtered-hidden)"
        )
        assert plain_sidechain.count() == 0, (
            "Non-memory sidechain messages should be hidden when isolating memory"
        )

    @pytest.mark.browser
    def test_memory_in_sidechain_timeline_lane(self, page: Page):
        """Regression (CR #204): a memory-in-sidechain interaction lands in the
        🧠 Memory timeline lane, not the Sub-assistant lane."""
        temp_file = self._html("Memory Sidechain Lane", MEMORY_SIDECHAIN_FIXTURE)
        page.goto(f"file://{temp_file}")

        page.locator("#toggleTimeline").click()
        page.wait_for_selector("#timeline-container", state="attached")
        page.wait_for_selector(".vis-timeline", timeout=30000)
        page.wait_for_selector(".vis-item", timeout=5000)

        memory_lane = page.locator('.vis-label:has-text("🧠 Memory")')
        memory_lane.first.wait_for(state="attached", timeout=10000)
        assert memory_lane.count() > 0, (
            "Memory-in-sidechain should produce a 🧠 Memory lane"
        )

"""Unit tests for the minimal theme's gutter icons (``html/minimal_icons.py``).

Every tool the formatters know and every role / kind the gutter names maps
to a glyph (unknown and MCP tools to the generic one), every symbol a page
references is in its sprite, and the classic theme carries none of it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from claude_code_log.converter import load_directory_transcripts, load_transcript
from claude_code_log.factories.tool_factory import TOOL_INPUT_MODELS
from claude_code_log.html import minimal_icons
from claude_code_log.html.minimal_icons import (
    GLYPH_NAMES,
    GLYPHS,
    TOOL_ICONS,
    icon_markup,
    icon_sprite,
    role_icon,
    tool_icon,
)
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.html.utils import CSS_CLASS_REGISTRY
from claude_code_log.models import MessageMeta, UserMemoryMessage, UserTextMessage
from test.dag_demo_fixture import write_dag_demo, write_team_demo, write_workflow_demo

TEST_DATA = Path(__file__).parent / "test_data"

_USE = re.compile(r"<use href='#(mi-[a-z-]+)'/>")
_SYMBOL = re.compile(r'<symbol id="(mi-[a-z-]+)"')


def _message(css_classes: str = "", **content: Any) -> Any:
    return SimpleNamespace(content=SimpleNamespace(**content), css=css_classes)


def _icon(css_classes: str, **content: Any) -> str:
    return role_icon(_message(**content), css_classes)


class TestTools:
    @pytest.mark.parametrize("name", sorted(TOOL_INPUT_MODELS))
    def test_every_typed_tool_has_its_own_glyph(self, name: str) -> None:
        icon = tool_icon(name)
        assert icon != "tool", f"{name} falls back to the generic glyph"
        assert icon in GLYPHS

    @pytest.mark.parametrize("name", sorted(TOOL_ICONS))
    def test_every_mapped_glyph_exists(self, name: str) -> None:
        assert TOOL_ICONS[name] in GLYPHS

    @pytest.mark.parametrize(
        "name",
        [
            "mcp__github__search_code",
            "mcp__openaiDeveloperDocs__search_openai_docs",
            "SomeBrandNewTool",
            "",
            None,
        ],
    )
    def test_mcp_and_unknown_tools_are_generic(self, name: Any) -> None:
        assert tool_icon(name) == "tool"
        assert _icon("tool_use", tool_name=name) == "tool"

    def test_tools_are_distinguishable(self) -> None:
        """Only genuinely related tools share a glyph."""
        shared: dict[str, set[str]] = {}
        for name, icon in TOOL_ICONS.items():
            shared.setdefault(icon, set()).add(name)
        groups = {frozenset(names) for names in shared.values() if len(names) > 1}
        assert groups == {
            frozenset({"Edit", "NotebookEdit"}),
            frozenset({"Bash", "PowerShell"}),
            frozenset({"Task", "Agent"}),
            frozenset({"AskUserQuestion", "ask_user_question"}),
            frozenset({"ExitPlanMode", "EnterPlanMode"}),
            frozenset({"CronCreate", "CronList", "CronDelete"}),
            frozenset({"TeamCreate", "TeamDelete"}),
        }

    @pytest.mark.parametrize(
        ("name", "icon"),
        [
            ("Read", "memory"),
            ("Write", "memory-write"),
            ("Edit", "memory-write"),
            ("MultiEdit", "memory-write"),
            ("Bash", "bash"),  # never a memory tool: its own glyph
        ],
    )
    def test_memory_file_variants(self, name: str, icon: str) -> None:
        assert tool_icon(name, memory=True) == icon
        assert _icon("tool_use memory", tool_name=name) == icon

    def test_tool_without_memory_modifier_keeps_its_glyph(self) -> None:
        assert _icon("tool_use", tool_name="Read") == "read"


class TestRoles:
    @pytest.mark.parametrize(
        ("css_classes", "icon"),
        [
            ("user", "user"),
            ("user steering", "steer"),
            ("user slash-command", "command"),
            ("user command-output", "output"),
            ("bash-input", "bash-in"),
            ("bash-output", "output"),
            ("user compacted", "compacted"),
            ("user teammate", "teammate"),
            ("user task-notification", "async"),
            ("user sidechain agent-depth-1", "user"),
            ("assistant", "assistant"),
            ("assistant sidechain agent-depth-1", "agent"),
            ("thinking", "thinking"),
            ("thinking sidechain", "thinking"),
            ("system system-info", "info"),
            ("system system-warning", "warning"),
            ("system system-error", "error"),
            ("system system-hook", "hook"),
            ("system system-hook-attachment", "hook"),
            ("system system-away-summary", "recap"),
            ("tool_result", "result"),
            ("tool_result error", "tool-error"),
            ("tool_result memory", "result"),
            ("tool_use workflow_phase", "phase"),
            ("tool_use workflow_agent", "agent"),
            ("image", "image"),
            ("unknown", "info"),
        ],
    )
    def test_every_kind_maps_to_a_glyph(self, css_classes: str, icon: str) -> None:
        assert _icon(css_classes) == icon
        assert icon in GLYPHS

    def test_user_memory_message(self) -> None:
        meta = MessageMeta(session_id="s", timestamp="t", uuid="u")
        memory = SimpleNamespace(
            content=UserMemoryMessage(meta=meta, memory_text="remember")
        )
        text = SimpleNamespace(content=UserTextMessage(meta=meta))
        assert role_icon(cast(Any, memory), "user") == "memory-write"
        assert role_icon(cast(Any, text), "user") == "user"

    def test_every_registered_content_class_gets_a_glyph(self) -> None:
        """Whatever classes the registry gives a card, with any modifier the
        renderer can add, the icon is one the sprite defines."""
        modifiers = ["", "sidechain", "error", "memory", "system-info", "system-error"]
        for classes in CSS_CLASS_REGISTRY.values():
            for modifier in modifiers:
                css = " ".join([*classes, modifier]).strip()
                assert _icon(css, tool_name="Read") in GLYPHS, css

    def test_branch_glyph(self) -> None:
        assert "branch" in GLYPHS


class TestSprite:
    def test_names_cover_every_glyph(self) -> None:
        assert set(GLYPH_NAMES) == set(GLYPHS)

    def test_every_glyph_is_used(self) -> None:
        used = set(TOOL_ICONS.values()) | {
            "tool",
            "memory",
            "memory-write",
            "branch",
        }
        for css in (
            "user",
            "user steering",
            "user slash-command",
            "user command-output",
            "bash-input",
            "user compacted",
            "user teammate",
            "user task-notification",
            "assistant",
            "assistant sidechain",
            "thinking",
            "system",
            "system system-warning",
            "system system-error",
            "system system-hook",
            "system system-away-summary",
            "tool_result",
            "tool_result error",
            "tool_use workflow_phase",
            "image",
        ):
            used.add(_icon(css))
        assert set(GLYPHS) - used == set()

    def test_sprite_is_well_formed_and_defines_each_glyph_once(self) -> None:
        sprite = str(icon_sprite())
        root = ET.fromstring(sprite)
        ids = [
            symbol.get("id")
            for symbol in root
            if symbol.tag == "{http://www.w3.org/2000/svg}symbol"
        ]
        assert ids == [f"mi-{name}" for name in GLYPHS]
        assert all(symbol.get("viewBox") == "0 0 16 16" for symbol in root)
        assert root.get("aria-hidden") == "true"

    def test_sprite_stays_small(self) -> None:
        assert len(str(icon_sprite())) < 10_000

    def test_icon_markup(self) -> None:
        assert str(icon_markup("read")) == (
            "<svg class='mn-ic' aria-hidden='true'><use href='#mi-read'/></svg>"
        )
        # An unknown name never references a symbol that does not exist.
        assert "#mi-tool'" in str(icon_markup("nope"))


def _render(source: Path, theme: str) -> str:
    if source.is_dir():
        entries, tree = load_directory_transcripts(source, silent=True)
    else:
        entries, tree = load_transcript(source, silent=True), None
    renderer = HtmlRenderer()
    renderer.theme = theme
    return renderer.generate(entries, "Icons", session_tree=tree)


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("icons")
    return {
        "representative": TEST_DATA / "representative_messages.jsonl",
        "edge": TEST_DATA / "edge_cases.jsonl",
        "sidechain": TEST_DATA / "sidechain.jsonl",
        "fork": TEST_DATA / "dag_within_fork.jsonl",
        "demo": write_dag_demo(tmp / "demo"),
        "team": write_team_demo(tmp / "team"),
        "workflow": write_workflow_demo(tmp / "workflow"),
    }


class TestPages:
    @pytest.mark.parametrize(
        "name",
        ["representative", "edge", "sidechain", "fork", "demo", "team", "workflow"],
    )
    def test_every_referenced_symbol_is_in_the_sprite(
        self, sources: dict[str, Path], name: str
    ) -> None:
        html = _render(sources[name], "minimal")
        assert html.count("class='mn-sprite'") == 1
        defined = set(_SYMBOL.findall(html))
        used = _USE.findall(html)
        assert used, "no icons rendered"
        assert set(used) <= defined
        # The sprite sits outside #transcript (a live swap keeps it).
        assert html.index("class='mn-sprite'") < html.index('<div id="transcript">')

    def test_every_row_has_one_icon_in_its_role_label(
        self, sources: dict[str, Path]
    ) -> None:
        html = _render(sources["demo"], "minimal")
        roles = re.findall(r"<span class='mn-role'>(.*?)</span>", html)
        assert roles
        for role in roles:
            assert len(_USE.findall(role)) == 1, role
            assert re.sub(r"<svg.*?</svg>", "", role).strip()  # label text kept

    def test_branch_header_carries_the_fork_glyph(
        self, sources: dict[str, Path]
    ) -> None:
        html = _render(sources["fork"], "minimal")
        assert "<use href='#mi-branch'/>" in html

    @pytest.mark.parametrize("name", ["representative", "fork", "demo"])
    def test_classic_has_no_icons(self, sources: dict[str, Path], name: str) -> None:
        html = _render(sources[name], "classic")
        assert "mn-ic" not in html
        assert "mn-sprite" not in html
        assert "#mi-" not in html


def test_module_documents_the_sprite_location() -> None:
    assert "outside" in (minimal_icons.__doc__ or "")

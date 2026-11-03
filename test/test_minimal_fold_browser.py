"""Browser tests for the minimal theme's collapse previews and fold depth (P4).

Covers work/minimal-theme-dag.md § 1.4 and § 1.5:

- collapse previews — the formatters' ``<details>`` (code, output, prose,
  params) show a clipped, faded preview with a ``+N lines`` label while
  closed, a ``− less`` line while open (plus a trailing ``− less`` on long
  bodies that scrolls the block back into view), and the classic 📋
  open/close-all still drives them;
- fold depth — the toolbar's Prompts / Steps / All control drives the
  classic fold-bar state machine, persists, yields to per-item overrides
  (the segment then shows no choice), survives a live update, and leaves
  the filter, search and timeline behaving as before;
- the cross-theme fix that came with it: in-page search starts even when
  storage is blocked.

The browser context is shared by every browser test (persistent, for the
HTTP cache), and so is ``file://`` localStorage: every test that can store
a depth clears it before and after.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import (
    _integrate_agent_entries,
    load_transcript,
    process_projects_hierarchy,
)
from claude_code_log.html.renderer import generate_html

TEST_DATA = Path(__file__).parent / "test_data"
REPRESENTATIVE = TEST_DATA / "representative_messages.jsonl"
NESTED_TRUNK = (
    TEST_DATA / "nested_agents" / "33330000-0000-4000-8000-000000000001.jsonl"
)

DEPTH_KEY = "claude-code-log:fold-depth"
COLLAPSIBLES = (
    "details.collapsible-code, details.collapsible-details, "
    "details.tool-param-collapsible"
)
BLOCK_STORAGE = """
Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() { throw new DOMException('blocked', 'SecurityError'); },
});
"""

# ---------------------------------------------------------------- fixture

SID = "c011a95e-0000-4000-8000-000000000001"
THINK_LINES = 15
ANSWER_LINES = 35
CODE_LINES = 30
BASH_LINES = 20


def _base(uuid: str, parent: str | None, minute: int) -> dict[str, Any]:
    return {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp/p4",
        "sessionId": SID,
        "version": "2.1.0",
        "uuid": uuid,
        "timestamp": f"2026-09-01T10:{minute:02d}:00.000Z",
    }


def _user(uuid: str, parent: str | None, minute: int, text: str) -> dict[str, Any]:
    entry = _base(uuid, parent, minute)
    entry["type"] = "user"
    entry["message"] = {"role": "user", "content": [{"type": "text", "text": text}]}
    return entry


def _assistant(
    uuid: str, parent: str, minute: int, content: list[dict[str, Any]]
) -> dict[str, Any]:
    entry = _base(uuid, parent, minute)
    entry["type"] = "assistant"
    entry["message"] = {
        "model": "claude-opus-4-7",
        "id": f"msg_{uuid}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    return entry


def _result(
    uuid: str,
    parent: str,
    minute: int,
    tool_id: str,
    content: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = _base(uuid, parent, minute)
    entry["type"] = "user"
    entry["message"] = {
        "role": "user",
        "content": [
            {"tool_use_id": tool_id, "type": "tool_result", "content": content}
        ],
    }
    if extra:
        entry["toolUseResult"] = extra
    return entry


def _turn_entries(prefix: str = "", start: int = 0) -> list[dict[str, Any]]:
    """One turn with every kind of collapsible, then a short second turn.

    Thinking (15 lines), a long answer (35 lines), a Read result (30 lines
    of Python), Bash output (20 lines) and a tool whose long parameter folds
    in the params table.
    """
    p = prefix
    think = "\n".join(
        f"Thinking line {i}: weighing the options for step {i}."
        for i in range(1, THINK_LINES + 1)
    )
    answer = "\n".join(
        f"Answer line {i} with some **markdown** text."
        for i in range(1, ANSWER_LINES + 1)
    )
    code = [f"value_{i} = {i}  # line {i}" for i in range(1, CODE_LINES + 1)]
    bash = "\n".join(
        f"output line {i}: some text to make this long enough"
        for i in range(1, BASH_LINES + 1)
    )
    notes = "A long parameter value that keeps going so the table folds it: " + (
        "lorem ipsum " * 20
    )
    m = start
    return [
        _user(f"{p}u1", None, m, "Please look at the code"),
        _assistant(
            f"{p}a1",
            f"{p}u1",
            m + 1,
            [{"type": "thinking", "thinking": think, "signature": "x"}],
        ),
        _assistant(f"{p}a2", f"{p}a1", m + 2, [{"type": "text", "text": answer}]),
        _assistant(
            f"{p}a3",
            f"{p}a2",
            m + 3,
            [
                {
                    "type": "tool_use",
                    "id": f"{p}t_read",
                    "name": "Read",
                    "input": {"file_path": "/tmp/p4/values.py"},
                }
            ],
        ),
        _result(
            f"{p}r3",
            f"{p}a3",
            m + 4,
            f"{p}t_read",
            "\n".join(f"{i}\t{line}" for i, line in enumerate(code, 1)),
            {
                "type": "text",
                "file": {
                    "filePath": "/tmp/p4/values.py",
                    "content": "\n".join(code),
                    "numLines": CODE_LINES,
                    "startLine": 1,
                    "totalLines": CODE_LINES,
                },
            },
        ),
        _assistant(
            f"{p}a4",
            f"{p}r3",
            m + 5,
            [
                {
                    "type": "tool_use",
                    "id": f"{p}t_bash",
                    "name": "Bash",
                    "input": {"command": "ls -la", "description": "List files"},
                }
            ],
        ),
        _result(f"{p}r4", f"{p}a4", m + 6, f"{p}t_bash", bash),
        _assistant(
            f"{p}a5",
            f"{p}r4",
            m + 7,
            [
                {
                    "type": "tool_use",
                    "id": f"{p}t_misc",
                    "name": "Frobnicate",
                    "input": {"target": "x", "notes": notes},
                }
            ],
        ),
        _result(f"{p}r5", f"{p}a5", m + 8, f"{p}t_misc", "done"),
        _user(f"{p}u2", f"{p}r5", m + 9, "Thanks, now the second turn"),
        _assistant(
            f"{p}a6", f"{p}u2", m + 10, [{"type": "text", "text": "Short reply."}]
        ),
    ]


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    return path


def _render(tmp_path: Path, theme: str = "minimal") -> Path:
    source = _write_jsonl(tmp_path / "p4.jsonl", _turn_entries())
    html = generate_html(load_transcript(source, silent=True), "P4", theme=theme)
    out = tmp_path / f"p4-{theme}.html"
    out.write_text(html, encoding="utf-8")
    return out


def _render_nested(tmp_path: Path) -> Path:
    entries = load_transcript(NESTED_TRUNK, silent=True)
    _integrate_agent_entries(entries)
    out = tmp_path / "nested-minimal.html"
    out.write_text(generate_html(entries, "Nested", theme="minimal"), encoding="utf-8")
    return out


def _open(page: Page, path: Path) -> None:
    """Load a page with no stored fold depth (see the module docstring)."""
    page.goto(path.as_uri())
    page.evaluate(f"localStorage.removeItem('{DEPTH_KEY}')")
    page.reload()


@pytest.fixture
def clean_depth(page: Page):
    yield page
    try:
        page.evaluate(f"localStorage.removeItem('{DEPTH_KEY}')")
    except Exception:  # page already closed or navigated away
        pass


def _after(page: Page, selector: str) -> str:
    return page.evaluate(
        "(sel) => getComputedStyle(document.querySelector(sel), '::after').content",
        selector,
    )


def _pressed(page: Page) -> dict[str, str]:
    return page.evaluate(
        "Object.fromEntries([...document.querySelectorAll('[data-mn-depth]')]"
        ".map(b => [b.dataset.mnDepth, b.getAttribute('aria-pressed')]))"
    )


# Per main-lane user turn: is its children container shown?
TURNS_OPEN_JS = """() => [...document.querySelectorAll(
    '#transcript .message.user:not(.sidechain)')]
    .filter(card => card.querySelector(':scope > .fold-bar'))
    .map(card => card.parentElement.querySelector(':scope > .children').style.display !== 'none')"""

VISIBLE_JS = (
    """(sel) => [...document.querySelectorAll(sel)].map(el => el.checkVisibility())"""
)

OPEN_JS = f"""() => [...document.querySelectorAll('#transcript :is({COLLAPSIBLES})')]
    .map(d => d.open)"""

READ = "#transcript .read-tool-result details.collapsible-code"
BASH = "#transcript .message.tool_result details.collapsible-details"
THINK = "#transcript .thinking-content details.collapsible-code"
ANSWER = "#transcript .assistant-text details.collapsible-code"
PARAM = "#transcript details.tool-param-collapsible"


# ---------------------------------------------------------- collapse


@pytest.mark.browser
class TestCollapsePreviews:
    def test_closed_previews_are_clipped_faded_and_labelled(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        clips = page.evaluate(
            """(sels) => sels.map(sel => {
                const d = document.querySelector(sel);
                const preview = d.querySelector(':scope > summary > .preview-content');
                const style = getComputedStyle(preview);
                return {
                    open: d.open,
                    height: preview.getBoundingClientRect().height,
                    font: parseFloat(style.fontSize),
                    clipped: preview.scrollHeight > preview.clientHeight,
                    mask: style.maskImage || style.webkitMaskImage,
                };
            })""",
            [READ, BASH, THINK, ANSWER],
        )
        # Code and output: 4.4em; prose (thinking, answers): 2.8em.
        for clip, em in zip(clips, (4.4, 4.4, 2.8, 2.8)):
            assert clip["open"] is False
            assert clip["height"] <= clip["font"] * em + 0.5, clip
            assert clip["clipped"], clip
            assert "linear-gradient" in clip["mask"], clip

        # "+N lines" equals the block's real line count.
        assert _after(page, f"{READ} > summary") == f'"+ {CODE_LINES} lines"'
        expect(page.locator(f"{READ} > summary > .line-count")).to_have_text(
            f"{CODE_LINES} lines"
        )
        assert _after(page, f"{BASH} > summary") == f'"+ {BASH_LINES} lines"'
        assert _after(page, f"{THINK} > summary") == f'"+ {THINK_LINES} lines"'
        assert _after(page, f"{ANSWER} > summary") == f'"+ {ANSWER_LINES} lines"'
        # A long single-line parameter: nothing to count but "more".
        assert _after(page, f"{PARAM} > summary") == '"+ more"'
        # The labels are generated content: search and the timeline index
        # textContent, which must not pick them up.
        assert "+ 30 lines" not in page.evaluate(
            "document.getElementById('transcript').textContent"
        )

    def test_open_shows_everything_and_less_scrolls_back(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        page.set_viewport_size({"width": 1100, "height": 500})
        _open(page, _render(tmp_path))
        summary = page.locator(f"{READ} > summary")
        preview_height = page.evaluate(
            f"document.querySelector('{READ} .preview-content')"
            ".getBoundingClientRect().height"
        )
        summary.click()
        details = page.locator(READ)
        expect(details).to_have_js_property("open", True)
        assert _after(page, f"{READ} > summary") == '"− less"'
        expect(page.locator(f"{READ} > summary > .preview-content")).to_be_hidden()
        full = page.locator(f"{READ} > .code-full")
        expect(full).to_be_visible()
        assert full.bounding_box()["height"] > preview_height * 3  # type: ignore[index]
        expect(full).to_contain_text(f"value_{CODE_LINES} = {CODE_LINES}")

        # Long bodies end with their own "− less".
        less = page.locator(f"{READ} > .mn-less")
        expect(less).to_be_visible()
        assert (
            page.evaluate(
                f"getComputedStyle(document.querySelector('{READ} > .mn-less'),"
                " '::before').content"
            )
            == '"− less"'
        )
        less.scroll_into_view_if_needed()
        page.evaluate(
            f"document.querySelector('{READ} > .mn-less').scrollIntoView({{block: 'end'}})"
        )
        geometry = "() => [document.querySelector('" + READ + "')"
        geometry += (
            ".getBoundingClientRect().top, document.querySelector('.mn-toolbar')"
        )
        geometry += ".getBoundingClientRect().bottom]"
        top, bar = page.evaluate(geometry)
        assert top < bar, "the block's top should have scrolled under the toolbar"

        less.click()
        expect(details).to_have_js_property("open", False)
        top, bar = page.evaluate(geometry)
        assert bar <= top <= bar + 40, (top, bar)
        assert _after(page, f"{READ} > summary") == f'"+ {CODE_LINES} lines"'

    def test_short_blocks_have_no_trailing_less(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        # 30, 15, 35 and 20 lines all qualify; the one-line param doesn't.
        assert page.locator("#transcript .mn-less").count() == 4
        expect(page.locator(f"{PARAM} > .mn-less")).to_have_count(0)

    def test_params_fold_keeps_its_key_toggle(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        param = page.locator(PARAM)
        key = param.locator(
            "xpath=ancestor::tr[1]/td[contains(@class,'tool-param-key')]/button"
        )
        key.click()
        expect(param).to_have_js_property("open", True)
        # Keyed row: the key's glyph is the close control, so the open
        # summary stays hidden (classic behaviour) and the body shows.
        expect(param.locator(":scope > summary")).to_be_hidden()
        expect(param).to_contain_text("lorem ipsum")
        key.click()
        expect(param).to_have_js_property("open", False)
        expect(param.locator(":scope > summary")).to_be_visible()

    def test_toggle_all_details_still_works(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator(".mn-more > summary").click()
        button = page.locator("#toggleDetails")
        expect(button).to_have_attribute("title", "Open all details")
        button.click()
        assert set(page.evaluate(OPEN_JS)) == {True}
        expect(button).to_have_attribute("title", "Close all details")
        assert _after(page, f"{BASH} > summary") == '"− less"'
        # A bulk override: the depth segment no longer claims a choice.
        assert set(_pressed(page).values()) == {"false"}
        button.click()
        assert set(page.evaluate(OPEN_JS)) == {False}

    def test_labels_return_after_a_rehydrate(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        """A live update replaces cards with fresh server markup."""
        page = clean_depth
        _open(page, _render(tmp_path))
        page.evaluate(
            f"""() => {{
                const card = document.querySelector('{BASH}').closest('.message');
                const fresh = card.cloneNode(true);
                fresh.querySelectorAll('[data-more]').forEach(el => el.removeAttribute('data-more'));
                fresh.querySelectorAll('.mn-less').forEach(el => el.remove());
                card.replaceWith(fresh);
                window.claudeLogRehydrate(fresh);
            }}"""
        )
        assert _after(page, f"{BASH} > summary") == f'"+ {BASH_LINES} lines"'
        expect(page.locator(f"{BASH} > .mn-less")).to_have_count(1)

    def test_line_count_labels_without_javascript_decoration(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        """No `data-more` (no JS): the formatter's count is the label."""
        page = clean_depth
        _open(page, _render(tmp_path))
        page.evaluate(
            "document.querySelectorAll('[data-more]')"
            ".forEach(el => el.removeAttribute('data-more'))"
        )
        count = page.locator(f"{READ} > summary > .line-count")
        expect(count).to_be_visible()
        assert (
            page.evaluate(
                f"getComputedStyle(document.querySelector('{READ} > summary > .line-count'),"
                " '::before').content"
            )
            == '"+ "'
        )
        assert _after(page, f"{READ} > summary") == "none"
        assert _after(page, f"{BASH} > summary") == '"+ more"'


# ---------------------------------------------------------- fold depth


@pytest.mark.browser
class TestFoldDepth:
    def test_steps_is_the_default(self, clean_depth: Page, tmp_path: Path) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        assert _pressed(page) == {"prompts": "false", "steps": "true", "all": "false"}
        assert set(page.evaluate(TURNS_OPEN_JS)) == {True}
        # Main-lane steps visible, every block a preview.
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.assistant")) == {
            True
        }
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.tool_result")) == {
            True
        }
        assert set(page.evaluate(OPEN_JS)) == {False}

    def test_prompts_folds_every_turn(self, clean_depth: Page, tmp_path: Path) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("[data-mn-depth='prompts']").click()
        assert _pressed(page)["prompts"] == "true"
        assert set(page.evaluate(TURNS_OPEN_JS)) == {False}
        displays = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message.user:not(.sidechain)')]
                .filter(card => card.querySelector(':scope > .fold-bar'))
                .map(card => getComputedStyle(
                    card.parentElement.querySelector(':scope > .children')).display)"""
        )
        assert displays and set(displays) == {"none"}
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.assistant")) == {
            False
        }
        # The prompts themselves (and the session header) stay.
        assert set(
            page.evaluate(VISIBLE_JS, "#transcript .message.user:not(.sidechain)")
        ) == {True}
        assert set(page.evaluate(VISIBLE_JS, "#transcript .session-header")) == {True}
        # The fold bars say what they show.
        expect(
            page.locator("#transcript .message.user > .fold-bar .fold-one-level").first
        ).to_have_class(re.compile(r"\bfolded\b"))

    def test_all_opens_everything(self, clean_depth: Page, tmp_path: Path) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("[data-mn-depth='all']").click()
        assert _pressed(page)["all"] == "true"
        assert set(page.evaluate(TURNS_OPEN_JS)) == {True}
        assert set(page.evaluate(OPEN_JS)) == {True}
        expect(page.locator("#toggleDetails")).to_have_attribute(
            "title", "Close all details"
        )
        page.locator("[data-mn-depth='steps']").click()
        assert set(page.evaluate(OPEN_JS)) == {False}
        expect(page.locator("#toggleDetails")).to_have_attribute(
            "title", "Open all details"
        )

    def test_choice_survives_a_reload(self, clean_depth: Page, tmp_path: Path) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("[data-mn-depth='prompts']").click()
        assert page.evaluate(f"localStorage.getItem('{DEPTH_KEY}')") == "prompts"
        page.reload()
        assert _pressed(page)["prompts"] == "true"
        assert set(page.evaluate(TURNS_OPEN_JS)) == {False}
        page.locator("[data-mn-depth='all']").click()
        page.reload()
        assert _pressed(page)["all"] == "true"
        assert set(page.evaluate(OPEN_JS)) == {True}

    def test_fold_bar_click_overrides_the_depth(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("[data-mn-depth='prompts']").click()
        first_turn = page.locator(
            "#transcript .message.user:not(.sidechain) > .fold-bar .fold-one-level"
        ).first
        first_turn.click()
        # That turn opens; the others stay folded; no segment is on.
        assert page.evaluate(TURNS_OPEN_JS) == [True, False]
        assert set(_pressed(page).values()) == {"false"}
        # Choosing a depth again resets the override.
        page.locator("[data-mn-depth='prompts']").click()
        assert page.evaluate(TURNS_OPEN_JS) == [False, False]
        assert _pressed(page)["prompts"] == "true"

    def test_opening_a_preview_keeps_the_depth(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator(f"{BASH} > summary").click()
        expect(page.locator(BASH)).to_have_js_property("open", True)
        assert _pressed(page)["steps"] == "true"

    def test_steps_folds_sub_agents_and_all_opens_them(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render_nested(tmp_path))
        spawns_js = """() => [...document.querySelectorAll('#transcript .children')]
            .filter(c => c.querySelector(':scope > .message-node > .message.sidechain'))
            .map(c => c.style.display !== 'none')"""
        # Every sub-agent transcript folded; the main lane open.
        assert set(page.evaluate(spawns_js)) == {False}
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.sidechain")) == {
            False
        }
        assert set(
            page.evaluate(VISIBLE_JS, "#transcript .message.assistant:not(.sidechain)")
        ) == {True}
        page.locator("[data-mn-depth='all']").click()
        assert set(page.evaluate(spawns_js)) == {True}
        # Since P6 the Branches mode, not the depth, decides whether a
        # sub-agent's transcript shows (spec § 1.5): at All the containers
        # are open, but "Main only" (the default) keeps every lane folded…
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.sidechain")) == {
            False
        }
        # …and interleaving shows them whatever the depth.
        page.locator("[data-mn-branches='interleaved']").click()
        # nsintr01 is the turn's fourth top-level lane: behind "+N more" (P7).
        page.locator(".mn-bmore").click()
        page.locator("[data-lane-ref='agent-nsintr01'] .mn-bfold").click()
        page.locator("[data-mn-depth='steps']").click()
        assert set(page.evaluate(spawns_js)) == {False}
        shown = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message.sidechain')]
                .filter(el => window.claudeLogDag.mode(el.dataset.lane) === 'interleaved')
                .map(el => el.checkVisibility())"""
        )
        assert shown and set(shown) == {True}
        page.evaluate("localStorage.removeItem('claude-code-log:branches')")

    def test_deep_link_beats_a_stored_depth(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        """The depth is applied before deep links reveal their target."""
        page = clean_depth
        path = _render(tmp_path)
        _open(page, path)
        page.locator("[data-mn-depth='prompts']").click()
        target = page.evaluate(
            f"document.querySelector('{BASH}').closest('.message').id"
        )
        page.goto(path.as_uri() + f"#{target}")
        page.reload()
        assert _pressed(page)["prompts"] == "true"
        expect(page.locator(f"#{target}")).to_be_visible()

    def test_works_without_storage(self, page: Page, tmp_path: Path) -> None:
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.add_init_script(BLOCK_STORAGE)
        page.goto(_render(tmp_path).as_uri())
        assert _pressed(page)["steps"] == "true"
        page.locator("[data-mn-depth='prompts']").click()
        assert set(page.evaluate(TURNS_OPEN_JS)) == {False}
        assert errors == []


# ------------------------------------------------- filter / search / timeline


@pytest.mark.browser
class TestParity:
    def test_filter_still_hides_cards_at_every_depth(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="tool"]').click()
        tools = "#transcript :is(.message.tool_use, .message.tool_result)"
        for depth in ("all", "prompts", "steps"):
            page.locator(f"[data-mn-depth='{depth}']").click()
            hidden = page.evaluate(
                f"""() => [...document.querySelectorAll('{tools}')]
                    .map(m => m.classList.contains('filtered-hidden')
                        && getComputedStyle(m).display === 'none')"""
            )
            assert hidden and set(hidden) == {True}, depth
        # Filtering composes with the depth: Steps shows the rest.
        assert set(page.evaluate(VISIBLE_JS, "#transcript .message.assistant")) == {
            True
        }

    def test_search_reveals_a_match_inside_a_folded_preview(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("[data-mn-depth='prompts']").click()
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("value_29")
        match = page.locator("#transcript .message.search-match").first
        expect(match).to_be_visible()
        # The match sits past the preview: search opened its block.
        expect(page.locator(READ)).to_have_js_property("open", True)
        expect(match.locator(".search-highlight").first).to_be_visible()

    def test_timeline_is_independent_of_the_depth(
        self, clean_depth: Page, tmp_path: Path
    ) -> None:
        page = clean_depth
        _open(page, _render(tmp_path))
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-item", timeout=30000)
        page.wait_for_timeout(500)
        before = page.locator(".vis-item").count()
        page.locator("[data-mn-depth='prompts']").click()
        page.wait_for_timeout(300)
        assert page.locator(".vis-item").count() == before
        # The filter still drives it.
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="assistant"]').click()
        page.wait_for_timeout(500)
        assert 0 < page.locator(".vis-item").count() < before


# ---------------------------------------------------------- live update


@pytest.fixture
def live_minimal(tmp_path: Path):
    """A served minimal-theme project with a watcher, and its JSONL."""
    from claude_code_log.server import ArchiveServer
    from claude_code_log.watch import WatchEngine

    projects = tmp_path / "projects"
    project = projects / "-tmp-p4"
    project.mkdir(parents=True)
    jsonl = _write_jsonl(project / f"{SID}.jsonl", _turn_entries())

    def convert(_paths: object = None) -> None:
        process_projects_hierarchy(projects, silent=True, theme="minimal")

    convert()
    engine = WatchEngine(
        [projects],
        convert,
        quiet_period=0.1,
        max_latency=0.5,
        poll_interval=0.05,
        on_error=lambda exc: pytest.fail(f"watch conversion failed: {exc!r}"),
    )
    engine.prime()
    stop = threading.Event()
    thread = engine.run_in_thread(stop)
    server = ArchiveServer(projects, port=0)
    server.start()
    try:
        yield f"{server.url}/{project.name}/session-{SID}.html", jsonl
    finally:
        stop.set()
        thread.join(timeout=10)
        server.stop()


@pytest.mark.browser
class TestLiveUpdate:
    def test_depth_and_overrides_survive_updates(
        self, clean_depth: Page, live_minimal: tuple[str, Path]
    ) -> None:
        page = clean_depth
        url, jsonl = live_minimal
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(url)
        page.evaluate(f"localStorage.setItem('{DEPTH_KEY}', 'prompts')")
        page.reload()
        page.wait_for_selector("#transcript")
        assert set(page.evaluate(TURNS_OPEN_JS)) == {False}
        # A hand override on the first turn.
        page.locator(
            "#transcript .message.user:not(.sidechain) > .fold-bar .fold-one-level"
        ).first.click()
        assert page.evaluate(TURNS_OPEN_JS) == [True, False]

        # Two updates: the first is the container swap, the second a patch.
        for n, minute in ((1, 20), (2, 40)):
            prefix = f"live{n}-"
            with jsonl.open("a", encoding="utf-8") as f:
                for entry in _turn_entries(prefix, minute):
                    f.write(json.dumps(entry) + "\n")
            page.wait_for_function(
                f"() => !!document.querySelector('[data-uuid=\"{prefix}u2\"]')",
                timeout=30000,
            )
            turns = page.evaluate(TURNS_OPEN_JS)
            # The override kept, every other turn — old and new — folded.
            assert turns == [True] + [False] * (len(turns) - 1), (n, turns)
            assert len(turns) == 2 + 2 * n
            assert set(_pressed(page).values()) == {"false"}
            # New cards' previews are decorated too.
            labels = page.evaluate(
                f"""() => [...document.querySelectorAll(
                    '[data-uuid^="{prefix}"] summary')]
                    .map(s => s.getAttribute('data-more'))"""
            )
            assert labels and None not in labels, labels
        assert errors == []

    def test_all_opens_new_cards_previews(
        self, clean_depth: Page, live_minimal: tuple[str, Path]
    ) -> None:
        page = clean_depth
        url, jsonl = live_minimal
        page.goto(url)
        page.evaluate(f"localStorage.setItem('{DEPTH_KEY}', 'all')")
        page.reload()
        page.wait_for_selector("#transcript")
        assert set(page.evaluate(OPEN_JS)) == {True}
        with jsonl.open("a", encoding="utf-8") as f:
            for entry in _turn_entries("liveall-", 20):
                f.write(json.dumps(entry) + "\n")
        page.wait_for_function(
            "() => !!document.querySelector('[data-uuid=\"liveall-u2\"]')",
            timeout=30000,
        )
        assert set(page.evaluate(OPEN_JS)) == {True}
        assert set(page.evaluate(TURNS_OPEN_JS)) == {True}


# ------------------------------------------- search with storage blocked


@pytest.mark.browser
class TestSearchWithoutStorage:
    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_in_page_search_starts(
        self, page: Page, tmp_path: Path, theme: str
    ) -> None:
        """search.html read its saved options unguarded: with storage blocked
        initSearch threw before binding anything, so typing did nothing."""
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.add_init_script(BLOCK_STORAGE)
        html = generate_html(
            load_transcript(REPRESENTATIVE, silent=True), "Blocked", theme=theme
        )
        out = tmp_path / f"blocked-{theme}.html"
        out.write_text(html, encoding="utf-8")
        page.goto(out.as_uri())
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("Python")
        expect(page.locator("#transcript .message.search-match").first).to_be_visible()
        expect(page.locator("#searchResultCount")).not_to_have_text("No results")
        # Toggling an option saves state: that write must not throw either.
        page.locator("#searchShowContext").check()
        expect(page.locator("#transcript .message.search-match").first).to_be_visible()
        assert errors == []

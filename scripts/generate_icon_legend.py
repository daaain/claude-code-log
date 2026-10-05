#!/usr/bin/env python3
"""Render the minimal theme's gutter icons as a legend image.

Writes ``docs/assets/themes/icons.png`` (light scheme): every glyph of
``claude_code_log/html/minimal_icons.py`` at its gutter size, in the colour
of the role that uses it, with its name. Needs Playwright's Chromium
(``uv run playwright install chromium``).

    uv run python scripts/generate_icon_legend.py [OUT.png] [--scale N] [--dark]

``--scale`` renders larger (the docs image is 2x, for sharp text) and
``--dark`` uses the dark tokens — handy for checking a glyph by eye.
"""

from __future__ import annotations

import argparse
import html
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_code_log.html.minimal_icons import (  # noqa: E402
    GLYPH_NAMES,
    GLYPHS,
    icon_markup,
    icon_sprite,
)

MINIMAL = ROOT / "claude_code_log" / "html" / "templates" / "components" / "minimal"

# The role colour each glyph appears in (tokens.css), for the legend.
_COLOURS = {
    "user": "--user",
    "steer": "--user",
    "command": "--user",
    "output": "--user",
    "bash-in": "--user",
    "compacted": "--user",
    "memory-write": "--user",
    "teammate": "--user",
    "async": "--note",
    "assistant": "--asst",
    "agent": "--ring",
    "thinking": "--muted",
    "info": "--sys",
    "warning": "--warn",
    "error": "--err",
    "hook": "--sys",
    "recap": "--sys",
    "image": "--user",
    "branch": "--lF",
    "tool-error": "--err",
}

# Groups, in legend order.
_GROUPS = (
    (
        "Messages",
        (
            "user",
            "steer",
            "command",
            "output",
            "bash-in",
            "compacted",
            "memory",
            "memory-write",
            "teammate",
            "async",
            "assistant",
            "agent",
            "thinking",
            "image",
            "branch",
        ),
    ),
    ("System", ("info", "warning", "error", "hook", "recap")),
    (
        "Tools",
        (
            "read",
            "write",
            "edit",
            "multiedit",
            "delete",
            "bash",
            "glob",
            "grep",
            "websearch",
            "webfetch",
            "taskoutput",
            "taskstop",
            "todo",
            "ask",
            "plan",
            "skill",
            "artifact",
            "monitor",
            "wakeup",
            "cron",
            "taskcreate",
            "taskupdate",
            "tasklist",
            "send",
            "workflow",
            "phase",
            "code",
            "wait",
            "result",
            "tool-error",
            "tool",
        ),
    ),
)


def legend_html(dark: bool) -> str:
    listed = [name for _, names in _GROUPS for name in names]
    missing = set(GLYPHS) - set(listed)
    if missing:
        raise SystemExit(f"glyphs missing from the legend: {sorted(missing)}")
    tokens = (MINIMAL / "tokens.css").read_text(encoding="utf-8")
    layout = (MINIMAL / "layout.css").read_text(encoding="utf-8")
    sections = []
    for title, names in _GROUPS:
        cells = "".join(
            f"<div class='cell' style='color: var({_COLOURS.get(name, '--tool')})'>"
            f"{icon_markup(name)}<span class='name'>{html.escape(GLYPH_NAMES[name])}</span></div>"
            for name in names
        )
        sections.append(f"<h2>{title}</h2><div class='grid'>{cells}</div>")
    theme = "dark" if dark else "light"
    return f"""<!DOCTYPE html><html data-theme='{theme}'><head><meta charset='utf-8'>
<style>{tokens}
{layout}
body.theme-minimal {{ margin: 0; padding: 16px 20px 18px; width: 760px; max-width: none;
    background: var(--bg); color: var(--fg); font: 13px var(--sans); }}
h2 {{ margin: 6px 0 6px; font: 600 11px var(--mono); color: var(--muted);
    text-transform: uppercase; letter-spacing: .06em; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 2px 12px; margin-bottom: 8px; }}
.cell {{ display: flex; align-items: center; gap: 7px; height: 22px; }}
.cell .mn-ic {{ flex: none; width: 13px; height: 13px; }}
.cell .name {{ font: 11.5px var(--mono); color: var(--fg); white-space: nowrap; }}
</style></head><body class='theme-minimal'>{icon_sprite()}{"".join(sections)}</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "out",
        nargs="?",
        default=str(ROOT / "docs" / "assets" / "themes" / "icons.png"),
    )
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--dark", action="store_true")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "legend.html"
        page_path.write_text(legend_html(args.dark), encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(
                viewport={"width": 800, "height": 400},
                device_scale_factor=args.scale,
            )
            page.goto(page_path.as_uri())
            page.locator("body").screenshot(path=args.out)
            browser.close()
    print(args.out)


if __name__ == "__main__":
    main()

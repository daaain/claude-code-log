#!/usr/bin/env python3
"""Time how long a rendered transcript takes to load and lay out, per theme.

The measurement behind dev-docs/minimal-theme.md § 5 ("Load"). Each
project (a directory of JSONL transcripts) is rendered as one combined page
in the classic and the minimal theme, as the browser tests do; each page is
then opened from ``file://`` in a fresh headless Chromium page at 1280x800,
and once ``load`` has fired the script waits for the next animation frame
and forces a layout. Reported, as medians over ``--runs``:

    laid      ms from navigation start to that point — the page laid out
    dcl/load  when DOMContentLoaded / load finished
    layouts   the renderer's layout and style-recalculation counts and time
              (Chrome's Performance domain), which show *why*: the minimal
              theme used to lay the transcript out nested while it parsed
              and then again as the DAG engine's grid

    uv run python scripts/bench_page_load.py            # the three P8 pages
    uv run python scripts/bench_page_load.py --runs 5 DIR [DIR ...]

Needs Chromium for Playwright (``uv run playwright install chromium``).
Absolute numbers depend on the machine; compare themes and revisions on
the same one.
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.html.renderer import HtmlRenderer

REAL = Path(__file__).resolve().parent.parent / "test" / "test_data" / "real_projects"
DEFAULT_PROJECTS = [
    REAL / "-Users-dain-workspace-claude-code-log-sample",
    REAL / "-Users-dain-workspace-coderabbit-review-helper",
    REAL / "-experiments-worktrees",
]
THEMES = ("classic", "minimal")

# One frame after `load`, then a forced layout: the page as the reader
# first sees it, laid out.
MEASURE = """() => new Promise(resolve => requestAnimationFrame(() => {
    document.body.offsetHeight;
    const nav = performance.getEntriesByType('navigation')[0];
    resolve({ laid: performance.now(), dcl: nav.domContentLoadedEventEnd,
              load: nav.loadEventEnd });
}))"""


def render(project: Path, out_dir: Path) -> dict[str, Path]:
    entries, tree = load_directory_transcripts(project, silent=True)
    pages: dict[str, Path] = {}
    for theme in THEMES:
        renderer = HtmlRenderer()
        renderer.theme = theme
        path = out_dir / f"{project.name}-{theme}.html"
        path.write_text(
            renderer.generate(entries, project.name, session_tree=tree),
            encoding="utf-8",
        )
        pages[theme] = path
    return pages


def measure(browser: Any, path: Path, runs: int) -> dict[str, float]:
    samples: list[dict[str, float]] = []
    for _ in range(runs):
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Performance.enable")
        page.goto(path.as_uri(), wait_until="load", timeout=180_000)
        sample: dict[str, float] = page.evaluate(MEASURE)
        metrics = {
            m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]
        }
        sample["layouts"] = metrics["LayoutCount"]
        sample["layout_s"] = metrics["LayoutDuration"]
        sample["styles"] = metrics["RecalcStyleCount"]
        sample["style_s"] = metrics["RecalcStyleDuration"]
        samples.append(sample)
        context.close()
    return {key: statistics.median(s[key] for s in samples) for key in samples[0]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Time page load to laid out, classic vs minimal theme."
    )
    parser.add_argument("projects", nargs="*", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch()
        for project in args.projects:
            for theme, path in render(project, Path(tmp)).items():
                r = measure(browser, path, args.runs)
                print(
                    f"{project.name[-28:]:28} {theme:8} "
                    f"{path.stat().st_size / 1e6:6.2f}MB  laid {r['laid'] / 1000:5.2f}s"
                    f"  dcl {r['dcl'] / 1000:5.2f}s  load {r['load'] / 1000:5.2f}s"
                    f"  layouts {r['layouts']:.0f} ({r['layout_s']:.2f}s)"
                    f"  styles {r['styles']:.0f} ({r['style_s']:.2f}s)",
                    flush=True,
                )
        browser.close()


if __name__ == "__main__":
    main()

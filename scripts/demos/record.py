#!/usr/bin/env python3
"""Record the demo videos: a short supercut and a chaptered walkthrough.

    just demos                          # everything
    uv run --group demos python scripts/demos/record.py --only branches
    uv run --group demos python scripts/demos/record.py --list

Each **scene** in ``SCENES`` is one clip: a fresh browser page driven by a
``Director`` (``director.py``), with its captions written next to the actions
they explain. Scenes come in two kinds:

* ``chapter`` — 20–40s on one feature, opening on a title card. The docs site
  embeds each on its own (``docs/demos.md``); joined, they are the
  walkthrough.
* ``beat`` — a few seconds of one highlight. Joined with cross-fades, between
  an opening and a closing card, they are the supercut the README shows.

Everything is recorded from the synthetic archive in ``demo_data.py``, served
by the real ``claude-code-log serve`` (the live chapter adds ``--watch`` over a
session that grows while it is filmed). To add a feature: write a scene,
register it, and add it to ``SUPERCUT`` or ``CHAPTERS``.

Outputs (``--out``, default ``.demos/``): ``clips/*.mp4`` per scene,
``supercut.mp4``, ``walkthrough.mp4`` and ``manifest.json`` (titles,
captions and durations). ``--publish`` copies the supercut and the chapters
to ``docs/assets/demos/``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FunctionType
from typing import Any, Callable, Optional

from playwright.sync_api import Route, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_data import (  # noqa: E402
    CI_SESSION,
    CWD,
    PROJECT,
    REPO,
    WATCH_SESSION,
    write_archive,
)
from director import (  # noqa: E402
    VIEWPORT,
    WIDE,
    Clip,
    Director,
    duration,
    join,
    scale,
)

THEME_SESSION = "dade0000-0000-4000-8000-000000000001"
DOCS_DEMOS = REPO / "docs" / "assets" / "demos"


# ---- the stage: archive + servers ------------------------------------------


class _Server:
    """``claude-code-log serve`` in a subprocess; ``url`` once it is up."""

    def __init__(self, projects: Path, cache: Path, *extra: str) -> None:
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "CLAUDE_CODE_LOG_CACHE_PATH": str(cache),
        }
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "claude_code_log.cli",
                "serve",
                "--projects-dir",
                str(projects),
                "--port",
                "0",
                "--theme",
                "minimal",
                *extra,
            ],  # fmt: skip
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        self.url = ""
        self._lines: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)
            match = re.search(r"(http://[^\s]+)/index\.html", line)
            if match and not self.url:
                self.url = match.group(1)

    def wait(self, needle: str = "", timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.url and (not needle or any(needle in line for line in self._lines)):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("serve did not start:\n" + "".join(self._lines[-30:]))

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Stage:
    """The demo archive, served; plus, on demand, a live session to watch."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        if workdir.exists():
            shutil.rmtree(workdir)
        self.projects = workdir / "projects"
        write_archive(self.projects)
        self.server = _Server(self.projects, workdir / "cache.db")
        self.server.wait()
        self._live: Optional[tuple[_Server, object]] = None

    def page(self, session: str = "") -> str:
        base = f"{self.server.url}/{PROJECT}"
        if not session:
            return f"{base}/combined_transcripts.html"
        return f"{base}/session-{session}.html"

    @property
    def index(self) -> str:
        return f"{self.server.url}/index.html"

    def live(self) -> tuple[str, Callable[[str], None]]:
        """A session that is still being written, under ``serve --watch``:
        returns its URL and ``advance(stage)`` (``test/dag_live_fixture``)."""
        if REPO.as_posix() not in sys.path:
            sys.path.insert(0, str(REPO))
        from test.dag_live_fixture import LIVE_SESSION, LiveDagScript

        if self._live:  # one live session at a time: start this one afresh
            self._live[0].close()
        projects = self.workdir / "live"
        if projects.exists():
            shutil.rmtree(projects)
        # "A minute ago", so the page reads the session as live.
        now = datetime.now(timezone.utc) - timedelta(minutes=1)
        base = now.replace(microsecond=(now.microsecond // 1000) * 1000)

        class Script(LiveDagScript):
            def _append(self, path: Path, entries: list[dict]) -> None:
                for entry in entries:  # the fixture's own cwd, as in demo_data
                    entry["cwd"] = CWD
                super()._append(path, entries)

        script = Script(projects / PROJECT, base=base)
        script.run_to("start")
        server = _Server(
            projects, self.workdir / "live-cache.db", "--watch", "--no-index"
        )
        server.wait("watching for changes")
        self._live = (server, script)
        return f"{server.url}/{PROJECT}/session-{LIVE_SESSION}.html", script.advance

    def close(self) -> None:
        self.server.close()
        if self._live:
            self._live[0].close()


# ---- scenes ------------------------------------------------------------------


@dataclass
class Scene:
    name: str
    kind: str  # "chapter" | "beat"
    title: str
    blurb: str
    run: Callable[[Director, Stage], None]
    viewport: dict[str, int]


SCENES: dict[str, Scene] = {}


def scene(
    kind: str, title: str, blurb: str, viewport: dict[str, int] = VIEWPORT
) -> Callable[[FunctionType], FunctionType]:
    def register(fn: FunctionType) -> FunctionType:
        name = fn.__name__.removeprefix("chapter_").removeprefix("beat_")
        SCENES[f"{kind}-{name}"] = Scene(name, kind, title, blurb, fn, viewport)
        return fn

    return register


def _open(
    d: Director, url: str, title: str = "", sub: str = "", kicker: str = ""
) -> None:
    """Load ``url`` before recording starts, optionally behind a title card
    that then fades away to reveal it."""
    d.goto(url, fade=False)
    if title:
        d.card(title, sub, kicker=kicker, instant=True)
    d.record()
    if title:
        d.wait(2.2)
        d.card(None, hold=0.5)


# Chapters ---------------------------------------------------------------------


@scene(
    "chapter",
    "Your archive",
    "The project index, its session finder and full-archive search.",
)
def chapter_archive(d: Director, stage: Stage) -> None:
    _open(
        d,
        stage.index,
        "Your archive",
        "Every project and session, one page",
        "Chapter 1",
    )
    d.caption("Every project Claude Code has worked in, newest first", hold=2.2)
    d.click("summary.mn-prow >> nth=0", after=1.2)
    d.caption("Each project lists its sessions, with their first prompt", hold=2.4)
    d.caption(
        "Or search every message in every project",
        sub="needs `claude-code-log serve`",
        hold=0.4,
    )
    d.click(".mn-archive-link", after=0.2, navigates=True)
    d.click("#q", after=0.2)
    d.type("cache key", after=2.2)
    d.caption("Hits link straight to the message, in context", hold=0.6)
    d.click("#results a >> nth=0", after=1.6, navigates=True)
    d.caption(None, hold=0.8)


@scene(
    "chapter",
    "Reading a session",
    "Prompts, thinking, tool calls with their results, diffs and highlighted code.",
)
def chapter_reading(d: Director, stage: Stage) -> None:
    _open(
        d,
        stage.page(CI_SESSION),
        "Reading a session",
        "Every prompt, thought, tool call and answer",
        "Chapter 2",
    )
    d.caption("The prompt, then each step Claude took, with when it happened", hold=2.6)
    d.spot(".message.thinking", hold=0.2)
    d.caption("Thinking, in the flow of the conversation", hold=2)
    d.spot(None)
    d.scroll_to(".message.tool_use:has-text('Run the cache tests')", after=0.6)
    d.caption(
        "Tool calls show their input and output together; failures in red", hold=2.2
    )
    d.click(".message.tool_result.error summary", after=1.4)
    d.scroll_to(".message.tool_use:has-text('cache.py, lines')", after=0.5)
    d.caption("Files read are syntax highlighted", hold=2)
    d.scroll_to(".edit-diff", offset=200, after=0.5)
    d.caption("Edits are diffs, with the changed characters marked", hold=2.4)
    # The answer near the top, so its table sits clear of the caption: it is
    # the last message, so give the page room to scroll that far.
    d.page.evaluate("document.body.style.paddingBottom = '50vh'")
    d.scroll_to(".message.assistant >> nth=-1", offset=110, after=1.2)
    d.caption("Answers render as markdown: headings, code, tables", hold=2.6)


@scene(
    "chapter",
    "Fold, filter, follow",
    "Fold depth, message filters, the timeline and dark mode.",
)
def chapter_toolbar(d: Director, stage: Stage) -> None:
    _open(
        d,
        stage.page(WATCH_SESSION),
        "Fold, filter, follow",
        "See as much or as little as you need",
        "Chapter 3",
    )
    d.caption("`Steps` shows each prompt with the steps that answered it", hold=2.2)
    d.click("[data-mn-depth='prompts']", after=0.4)
    d.caption("`Prompts` folds a session down to what you asked", hold=2)
    d.click("[data-mn-depth='all']", after=0.4)
    d.caption("`All` unfolds everything, down to tool results", hold=2.2)
    d.click("[data-mn-depth='steps']", after=0.6)
    d.click("#filterMessages", after=0.4)
    d.caption("Filters hide whole kinds of message", hold=1)
    d.click(".filter-toggle[data-type='tool']", after=1.6)
    d.click(".filter-toggle[data-type='tool']", after=0.6)
    d.click("#filterMessages", after=0.4)
    d.click("#toggleTimeline", after=1.6)
    d.caption("The timeline shows when each step ran; click to jump", hold=2.6)
    d.click("#toggleTimeline", after=0.4)
    d.click("[data-mn-theme='dark']", after=0.3)
    d.caption("Light, dark, or follow the system", hold=2.4)
    d.click("[data-mn-theme='auto']", after=0.8)


@scene(
    "chapter",
    "Agents and forks",
    "Sub-agents, background agents and rewound prompts as branch lanes.",
    WIDE,
)
def chapter_branches(d: Director, stage: Stage) -> None:
    _open(
        d,
        stage.page(THEME_SESSION),
        "Agents and forks",
        "Sub-agents and rewinds as branch lanes",
        "Chapter 4",
    )
    d.caption("Two background agents: each is a lane beside the main session", hold=2.6)
    d.click("button.mn-bfold >> nth=0", after=1.2)
    d.caption("Unfold one to read its steps in place", hold=1.8)
    d.click("[data-mn-branches='interleaved']", after=0.4)
    d.caption("`Interleaved` merges every lane's steps in time order", hold=2.4)
    d.click("[data-mn-branches='columns']", after=0.4)
    d.caption("`Columns` lays the agents out side by side", hold=1.6)
    d.scroll_by(420, after=1.2)
    d.pan(700, after=0.4)
    d.caption("…nested agents and the rewound fork included", hold=2.2)
    d.pan(-700, after=0.6)
    d.click("[data-mn-branches='main']", after=0.4)
    d.scroll_to(".fork-point", offset=260, after=0.6)
    d.caption("A rewound prompt forks: both branches are kept", hold=2.8)


@scene("chapter", "Search", "In-page search, including steps that are folded away.")
def chapter_search(d: Director, stage: Stage) -> None:
    _open(
        d,
        stage.page(THEME_SESSION),
        "Search",
        "Find anything, even when it's folded away",
        "Chapter 5",
    )
    d.caption("Press `/` to search the page", hold=1.2)
    d.press("/", after=0.4)
    d.type("github-dark", after=1.2)
    d.caption("Matches inside folded agent steps are found too", hold=2.2)
    d.press("Enter", after=1.4)
    d.press("Enter", after=1.6)
    d.caption("`Esc` brings everything back, and keeps your place", hold=0.8)
    d.press("Escape", after=2.4)


@scene(
    "chapter",
    "Live sessions",
    "With `serve --watch`, an open page follows a session as it runs.",
)
def chapter_live(d: Director, stage: Stage) -> None:
    url, advance = stage.live()
    _open(d, url, "Live sessions", "Keep a page open while Claude works", "Chapter 6")
    d.caption(
        "`claude-code-log serve --watch` regenerates pages as sessions grow", hold=2.6
    )
    d.spot(".message[data-lane-id='agent-c0live0sync']", hold=0.2)
    d.caption("A running agent's lane stays open, and grows…", hold=0.6)
    for step in ("c_grows", "c_grows_more"):
        advance(step)
        d.wait(2.2)
    d.caption("…until it answers and the lane closes", hold=0.4)
    advance("c_merges")
    d.wait(2.6)
    d.spot(None)
    d.caption("Background agents report back the same way", hold=0.4)
    advance("a_merges")
    d.wait(3)
    d.caption(None, hold=0.5)


# Beats (the supercut) ----------------------------------------------------------


@scene("beat", "Archive", "")
def beat_archive(d: Director, stage: Stage) -> None:
    _open(d, stage.index)
    d.caption("Every project and session in one place", hold=0.8)
    d.click("summary.mn-prow >> nth=0", after=2.2)


@scene("beat", "Reading", "")
def beat_reading(d: Director, stage: Stage) -> None:
    _open(d, stage.page(CI_SESSION))
    d.caption("Prompts, thinking, tool calls and diffs, readable", hold=0.6)
    d.scroll_by(700, after=1.4)
    d.scroll_by(700, after=1.6)


@scene("beat", "Fold depth", "")
def beat_depth(d: Director, stage: Stage) -> None:
    _open(d, stage.page(WATCH_SESSION))
    d.caption("Fold down to your prompts, or unfold every step", hold=0.4)
    d.click("[data-mn-depth='prompts']", after=1.2)
    d.click("[data-mn-depth='all']", after=1.6)


@scene("beat", "Branches", "", WIDE)
def beat_branches(d: Director, stage: Stage) -> None:
    _open(d, stage.page(THEME_SESSION))
    d.caption("Sub-agents and forks as branch lanes", hold=1.4)
    d.click("[data-mn-branches='interleaved']", after=1.4)
    d.click("[data-mn-branches='columns']", after=2.2)


@scene("beat", "Search", "")
def beat_search(d: Director, stage: Stage) -> None:
    _open(d, stage.page(THEME_SESSION))
    d.caption("Search finds it, even in folded steps", hold=0.3)
    d.press("/", after=0.3)
    d.type("github-dark", delay=0.06, after=0.8)
    d.press("Enter", after=1.8)


@scene("beat", "Dark mode", "")
def beat_dark(d: Director, stage: Stage) -> None:
    _open(d, stage.page(CI_SESSION))
    d.caption("Light or dark", hold=0.4)
    d.click("[data-mn-theme='dark']", after=2)


@scene("beat", "Live", "")
def beat_live(d: Director, stage: Stage) -> None:
    url, advance = stage.live()
    _open(d, url)
    d.caption("Follow a session live while Claude works", hold=1)
    advance("c_grows")
    d.wait(1.8)
    advance("c_grows_more")
    d.wait(1.6)
    advance("c_merges")
    d.wait(2)


CHAPTERS = ["archive", "reading", "toolbar", "branches", "search", "live"]
SUPERCUT = ["archive", "reading", "depth", "branches", "search", "dark", "live"]


@scene("beat", "Opening card", "")
def beat_intro(d: Director, stage: Stage) -> None:
    _open(d, stage.index)
    d.card(
        "claude-code-log",
        "Your Claude Code sessions, as pages you can read",
        kicker="",
        instant=True,
    )
    d.wait(2.8)


@scene("beat", "Closing card", "")
def beat_outro(d: Director, stage: Stage) -> None:
    _open(d, stage.index)
    d.card(
        "Try it",
        "Converts `~/.claude/projects` and opens the index",
        code="uvx claude-code-log --open-browser",
        instant=True,
    )
    d.wait(3.4)


# ---- running -------------------------------------------------------------------


def _cdn_cache(cache: Path) -> Callable[[Route], None]:
    """Serve unpkg requests (vis-timeline) from a local cache: recordings stop
    depending on the network, and browsers that distrust an intercepting
    proxy still get the timeline."""

    def handle(route: Route) -> None:
        url = route.request.url
        target = cache / re.sub(r"[^A-Za-z0-9._-]", "_", url)
        if not target.exists():
            cache.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=30) as response:
                target.write_bytes(response.read())
        kind = "text/css" if url.endswith(".css") else "application/javascript"
        route.fulfill(body=target.read_bytes(), content_type=kind)

    return handle


def record_clip(
    browser,
    stage: Stage,
    run: Callable[[Director, Stage], None],
    out: Path,
    workdir: Path,
    viewport: dict[str, int] = VIEWPORT,
) -> Path:
    context = browser.new_context(
        viewport=viewport, device_scale_factor=scale(viewport)
    )
    context.route("https://unpkg.com/**", _cdn_cache(workdir / "cdn"))
    try:
        director = Director(context, workdir / "frames")
        run(director, stage)
        return director.finish(out)
    finally:
        context.close()


def _select(only: Optional[list[str]]) -> list[str]:
    """Scene keys for ``--only`` names: ``branches`` means both the chapter
    and the beat; ``chapter-branches`` just the one."""
    if not only:
        return list(SCENES)
    keys = [k for k in SCENES if k in only or SCENES[k].name in only]
    unknown = set(only) - {k for k in keys} - {SCENES[k].name for k in keys}
    if unknown:
        raise SystemExit(f"unknown scenes: {', '.join(sorted(unknown))} (see --list)")
    return keys


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--only", nargs="*", help="scene names to (re-)record")
    parser.add_argument("--list", action="store_true", help="list the scenes")
    parser.add_argument("--out", type=Path, default=REPO / ".demos")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument(
        "--publish", action="store_true", help=f"copy the videos to {DOCS_DEMOS}"
    )
    args = parser.parse_args()

    if args.list:
        for key, s in SCENES.items():
            print(f"{key:18} {s.title}")
        return

    out: Path = args.out
    clips = out / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    keys = _select(args.only)
    stage = Stage(out / "stage")
    try:
        with sync_playwright() as p:
            # The screencast sends frames at CSS-pixel size whatever the
            # context's device scale factor; only the launch flag scales
            # them up, and it is browser-wide: one browser per scale.
            browsers: dict[float, Any] = {}

            def browser_for(viewport: dict[str, int]) -> Any:
                factor = scale(viewport)
                if factor not in browsers:
                    browsers[factor] = p.chromium.launch(
                        headless=not args.headed,
                        args=[f"--force-device-scale-factor={factor}"],
                    )
                return browsers[factor]

            for key in keys:
                target = clips / f"{key}.mp4"
                started = time.monotonic()
                record_clip(
                    browser_for(SCENES[key].viewport),
                    stage,
                    SCENES[key].run,
                    target,
                    out / "work",
                    SCENES[key].viewport,
                )
                print(
                    f"  {key:18} {duration(target):5.1f}s"
                    f"  (recorded in {time.monotonic() - started:.0f}s)"
                )
            for browser in browsers.values():
                browser.close()
    finally:
        stage.close()

    _assemble(out, args.publish)


def _assemble(out: Path, publish: bool) -> None:
    """Join the clips into the supercut and the walkthrough, once every clip
    each needs exists; write the manifest; optionally publish."""
    clips = out / "clips"
    beats = ["intro", *SUPERCUT, "outro"]
    supercut = [
        Clip(clips / f"beat-{name}.mp4", "smoothleft" if i % 2 else "fade")
        for i, name in enumerate(beats)
    ]
    chapters = [Clip(clips / f"chapter-{name}.mp4") for name in CHAPTERS]
    manifest: dict[str, object] = {}
    if all(c.path.exists() for c in supercut):
        join(supercut, out / "supercut.mp4", fade=0.5)
        manifest["supercut"] = round(duration(out / "supercut.mp4"), 1)
        print(f"supercut           {manifest['supercut']:5.1f}s")
    if all(c.path.exists() for c in chapters):
        join(chapters, out / "walkthrough.mp4", fade=0.6)
        manifest["walkthrough"] = round(duration(out / "walkthrough.mp4"), 1)
        print(f"walkthrough        {manifest['walkthrough']:5.1f}s")
        manifest["chapters"] = [
            {
                "file": f"{i}-{name}.mp4",
                "title": SCENES[f"chapter-{name}"].title,
                "blurb": SCENES[f"chapter-{name}"].blurb,
                "seconds": round(duration(clips / f"chapter-{name}.mp4"), 1),
            }
            for i, name in enumerate(CHAPTERS, start=1)
        ]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if publish:
        if "supercut" not in manifest or "chapters" not in manifest:
            raise SystemExit("--publish needs every clip: record without --only first")
        DOCS_DEMOS.mkdir(parents=True, exist_ok=True)
        shutil.copy(out / "supercut.mp4", DOCS_DEMOS / "supercut.mp4")
        for i, name in enumerate(CHAPTERS, start=1):
            shutil.copy(clips / f"chapter-{name}.mp4", DOCS_DEMOS / f"{i}-{name}.mp4")
        (REPO / "docs" / "demos.md").write_text(_docs_page(manifest), encoding="utf-8")
        print(f"published to {DOCS_DEMOS.relative_to(REPO)} and docs/demos.md")


def _video(src: str) -> str:
    return (
        f'<video controls muted playsinline preload="metadata" width="100%" '
        f'src="../assets/demos/{src}"></video>'
    )


def _docs_page(manifest: dict) -> str:
    """``docs/demos.md``: the supercut, then each chapter with its blurb."""
    minutes, seconds = divmod(round(manifest["walkthrough"]), 60)
    parts = [
        "<!-- Generated by scripts/demos/record.py --publish; edit the scenes"
        " there, not this file. -->\n",
        "# Demo videos\n",
        "The minimal theme in motion, recorded from a synthetic archive by"
        " `scripts/demos/record.py` (`just demos` re-records them).\n",
        "## Overview\n",
        f"{_video('supercut.mp4')}\n",
        f"## Walkthrough\n\nSix short chapters, {minutes}:{seconds:02d} in all.\n",
    ]
    for chapter in manifest["chapters"]:
        parts.append(
            f"### {chapter['title']}\n\n{chapter['blurb']}\n\n{_video(chapter['file'])}\n"
        )
    return "\n".join(parts)


if __name__ == "__main__":
    main()

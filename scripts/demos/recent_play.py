#!/usr/bin/env python3
"""Play the recent sessions demo's stages against a page you open yourself.

    just recent-play                 # the next stage every 20 s
    just recent-play --step          # press Enter for the next stage
    just recent-play --every 30 --no-open

The same stages the demo records and ``test/test_recent_live_browser.py``
checks (``test/recent_live_fixture.py``): sessions across four projects,
written as real JSONL into a sandbox projects directory and converted by
the real full run after each stage. Open the printed ``recent.html`` (it
opens in your browser by default) and watch the rows move, judge the
motion, try the filters while it runs.

The page reloads its data file every 15 s, as it does for real, so a stage
can take that long to show; switching to another tab and back reloads it
at once. In a dev box (foldyard) there is no browser: the default sandbox
is inside the repo, so open the printed repo-relative path on the host.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
import time
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from test.recent_live_fixture import DESCRIBE, STAGES, RecentLive  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--every", type=float, default=20, help="seconds between stages (default 20)"
    )
    parser.add_argument(
        "--step", action="store_true", help="wait for Enter between stages"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="don't open the page in a browser"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / ".demos" / "recent-play",
        help="the sandbox directory (emptied first; default .demos/recent-play)",
    )
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    live = RecentLive(args.out / "projects")

    def advance(stage: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):  # the run's progress
            live.advance(stage)
        print(f"{time.strftime('%H:%M:%S')}  {stage:12} {DESCRIBE[stage]}")

    advance(STAGES[0])
    print(f"\n  {live.url}")
    if live.page.is_relative_to(REPO):
        # In a dev box the URL is the box's path; this one is the same on
        # the host, which is where a browser is.
        print(f"  (in the repo: {live.page.relative_to(REPO)})")
    print()
    if not args.no_open and not webbrowser.open(live.url):
        print("  No browser here: open the page above on your own machine.\n")
    try:
        for stage in STAGES[1:]:
            if args.step:
                input("  Enter for the next stage… ")
            else:
                time.sleep(args.every)
            advance(stage)
    except (KeyboardInterrupt, EOFError):
        print()
        return
    print("\nThat's every stage; the page stays where it is. Run again to replay.")


if __name__ == "__main__":
    main()

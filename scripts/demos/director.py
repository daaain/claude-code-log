"""Record a browser scene as an MP4, with captions, a cursor and transitions.

A ``Director`` drives one Chromium page through a scene — ``caption``,
``click``, ``scroll_to``, ``type``, ``goto``, ``card`` — while a Chrome
DevTools Protocol screencast saves every frame the compositor produces
(lossless PNGs at a 1.5x device pixel ratio, so text stays crisp). Captions,
title cards, the cursor and the fades are drawn by an overlay injected into
the page itself (``OVERLAY_JS``), so the recording already carries them and
nothing is composited afterwards except the cross-fades between clips
(``join``).

Timing is real time: a scene that waits two seconds produces two seconds of
video. The screencast only emits a frame when the page changes, so frames are
encoded with their own timestamps and resampled to a constant 30 fps.
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page

# Every video is 1920x1200. A scene picks the CSS viewport it is laid out
# in, and the device pixel ratio scales that up to the frame: narrower than
# a desktop window by default, so the transcript fills the frame and its
# text stays readable when the video is shown scaled down; ``WIDE`` where a
# layout needs the room (the Columns branch mode).
FRAME = (1920, 1200)
VIEWPORT = {"width": 1120, "height": 700}
WIDE = {"width": 1440, "height": 900}


def scale(viewport: dict[str, int]) -> float:
    """The device pixel ratio that renders ``viewport`` at ``FRAME`` size."""
    return FRAME[0] / viewport["width"]


FPS = 30
WINDOW_FLAG = "demo-veil"  # window.name survives navigation, across origins

OVERLAY_JS = r"""
(() => {
  if (window.__demo) return;
  const veiled = window.name === "%(flag)s";
  const host = document.createElement("div");
  host.id = "__demo-overlay";
  host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
  <style>
    :host { all: initial; }
    * { box-sizing: border-box; }
    .fade { transition: opacity .35s ease; }
    .veil { position: fixed; inset: 0; background: var(--veil, #f7f7f5); opacity: 0; }
    .caption {
      position: fixed; left: 50%%; bottom: 44px; transform: translate(-50%%, 8px);
      max-width: 1000px; padding: 12px 22px; border-radius: 12px;
      background: rgba(20, 20, 24, .86); color: #fff; opacity: 0;
      font: 500 21px/1.35 Inter, "Segoe UI", system-ui, -apple-system, sans-serif;
      letter-spacing: -.005em; text-align: center;
      box-shadow: 0 10px 30px rgba(0,0,0,.25);
      transition: opacity .3s ease, transform .3s ease;
    }
    .caption.on { opacity: 1; transform: translate(-50%%, 0); }
    .caption small { display: block; margin-top: 3px; font-size: 15px; font-weight: 400; opacity: .72; }
    .caption code { font: 500 .9em ui-monospace, "SF Mono", Menlo, monospace; background: rgba(255,255,255,.14); padding: 1px 6px; border-radius: 5px; }
    .card {
      position: fixed; inset: 0; display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 18px; opacity: 0;
      background: radial-gradient(120%% 90%% at 50%% 0%%, #2b2d42 0%%, #15161e 70%%);
      color: #f3f1ea; font-family: Inter, "Segoe UI", system-ui, -apple-system, sans-serif;
      text-align: center; padding: 0 80px;
    }
    .card .kicker { font: 600 15px/1 ui-monospace, "SF Mono", Menlo, monospace; letter-spacing: .14em; text-transform: uppercase; color: #d97757; }
    .card h1 { margin: 0; font-size: 56px; font-weight: 650; letter-spacing: -.025em; }
    .card p { margin: 0; max-width: 820px; font-size: 24px; line-height: 1.4; color: #c9c6bd; }
    .card code { font: 500 22px ui-monospace, "SF Mono", Menlo, monospace; color: #f3f1ea; background: rgba(255,255,255,.08); padding: 10px 18px; border-radius: 10px; }
    .cursor { position: fixed; left: 0; top: 0; width: 24px; height: 24px; opacity: 0; transition: opacity .2s; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
    .ripple { position: fixed; width: 34px; height: 34px; margin: -17px 0 0 -17px; border-radius: 50%%; border: 3px solid #d97757; opacity: 0; }
    .ripple.go { animation: ripple .5s ease-out; }
    @keyframes ripple { from { opacity: .9; transform: scale(.4); } to { opacity: 0; transform: scale(1.6); } }
    .spot { position: fixed; border-radius: 10px; border: 3px solid #d97757; opacity: 0; box-shadow: 0 0 0 4px rgba(217,119,87,.18); transition: opacity .3s ease, left .4s ease, top .4s ease, width .4s ease, height .4s ease; }
  </style>
  <div class="spot fade"></div>
  <div class="caption"></div>
  <div class="card fade"></div>
  <div class="veil fade"></div>
  <svg class="cursor" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.5 15 L11.5 22 L14.5 20.8 L11.5 14 L18 14 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>
  <div class="ripple"></div>`;
  const $ = (s) => root.querySelector(s);
  const veil = $(".veil"), caption = $(".caption"), card = $(".card"),
        cursor = $(".cursor"), ripple = $(".ripple"), spot = $(".spot");
  if (veiled) { veil.style.transition = "none"; veil.style.opacity = "1"; }
  const mount = () => {
    document.documentElement.appendChild(host);
    const bg = getComputedStyle(document.body || document.documentElement).backgroundColor;
    if (bg && bg !== "rgba(0, 0, 0, 0)") veil.style.setProperty("--veil", bg);
  };
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
  let cx = innerWidth / 2, cy = innerHeight / 2;
  const place = () => { cursor.style.transform = `translate(${cx - 4}px, ${cy - 2}px)`; };
  window.__demo = {
    veil(on) {
      veil.style.transition = "";
      const bg = getComputedStyle(document.body).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)") veil.style.setProperty("--veil", bg);
      veil.style.opacity = on ? "1" : "0";
      if (!on) window.name = "";
    },
    caption(html) {
      if (!html) { caption.classList.remove("on"); return; }
      const swap = () => { caption.innerHTML = html; caption.classList.add("on"); };
      if (caption.classList.contains("on")) {
        caption.classList.remove("on"); setTimeout(swap, 280);
      } else swap();
    },
    card(html, instant) {
      if (html) card.innerHTML = html;
      card.style.transition = instant ? "none" : "";
      card.style.opacity = html ? "1" : "0";
    },
    cursorShow(on) { cursor.style.opacity = on ? "1" : "0"; },
    cursorAt(x, y) { cx = x; cy = y; place(); },
    cursorTo(x, y, ms) {
      return new Promise((done) => {
        const x0 = cx, y0 = cy, t0 = performance.now();
        const ease = (t) => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        const step = (now) => {
          const t = Math.min(1, (now - t0) / ms), k = ease(t);
          cx = x0 + (x - x0) * k; cy = y0 + (y - y0) * k; place();
          if (t < 1) requestAnimationFrame(step); else done();
        };
        requestAnimationFrame(step);
      });
    },
    ripple() {
      ripple.style.left = cx + "px"; ripple.style.top = cy + "px";
      ripple.classList.remove("go"); void ripple.offsetWidth; ripple.classList.add("go");
    },
    spot(r) {
      if (!r) { spot.style.opacity = "0"; return; }
      const pad = 6;
      Object.assign(spot.style, { left: r.x - pad + "px", top: r.y - pad + "px",
        width: r.width + 2 * pad + "px", height: r.height + 2 * pad + "px", opacity: "1" });
    },
    get cursor() { return [cx, cy]; },
  };
})();
""" % {"flag": WINDOW_FLAG}


def ffmpeg() -> str:
    """The ffmpeg binary: a system one, else the ``imageio-ffmpeg`` wheel's."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Imported by name: the demos group is optional, so a static import
    # would be unresolved for the type checkers in a plain `uv sync`.
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
    except ImportError as exc:  # pragma: no cover - environment
        raise SystemExit(
            "ffmpeg not found: install it, or `uv sync --group demos`"
        ) from exc
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def duration(video: Path) -> float:
    """A video's duration in seconds, read from ffmpeg's probe output."""
    probe = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(video)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe.stderr)
    if not match:
        raise RuntimeError(f"no duration for {video}:\n{probe.stderr}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


@dataclass
class Clip:
    path: Path
    transition: str = "fade"  # xfade transition INTO this clip


@dataclass
class Director:
    """Drives one page through a scene and records it."""

    context: BrowserContext
    workdir: Path
    page: Page = field(init=False)
    _frames: list[tuple[float, Path]] = field(default_factory=list, init=False)
    _cdp: Any = field(default=None, init=False)
    _recording: bool = field(default=False, init=False)
    _size: tuple[int, int] = field(default=(0, 0), init=False)

    def __post_init__(self) -> None:
        self.context.add_init_script(OVERLAY_JS)
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)

    # ---- recording ---------------------------------------------------------

    def _on_frame(self, event: dict[str, Any]) -> None:
        self._cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        if not self._recording:
            return
        path = self.workdir / f"f{len(self._frames):06d}.png"
        data = base64.b64decode(event["data"])
        if not self._frames:
            # PNG IHDR width, height; checked in finish (an exception raised
            # in an event handler would not reach the scene).
            self._size = struct.unpack(">II", data[16:24])
        path.write_bytes(data)
        self._frames.append((event["metadata"]["timestamp"], path))

    def _start_screencast(self) -> None:
        self._cdp = self.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        self._cdp.send(
            "Page.startScreencast",
            {
                "format": "png",
                "maxWidth": FRAME[0],
                "maxHeight": FRAME[1],
                "everyNthFrame": 1,
            },
        )

    def record(self) -> None:
        """Start keeping frames (call once the first page is ready)."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        for old in self.workdir.glob("f*.png"):
            old.unlink()
        self._frames.clear()
        self._recording = True
        if self._cdp is None:
            self._start_screencast()
        # Nudge the compositor so the first frame lands at t=0 even on a
        # still page.
        self.page.evaluate(
            "document.documentElement.style.outline = '0 solid transparent'"
        )
        self.wait(0.05)

    def finish(self, out: Path, tail: float = 0.4) -> Path:
        """Stop recording and encode the frames to ``out`` (H.264 MP4)."""
        self.wait(tail)
        # The screencast sends nothing while the page is still, so a still
        # stretch at the end has no frame to carry it: hold the last frame
        # until now (frame timestamps are wall-clock seconds).
        end = time.time()
        self._recording = False
        if self._size != FRAME:
            raise RuntimeError(
                f"screencast frames are {self._size[0]}x{self._size[1]}, not "
                f"{FRAME[0]}x{FRAME[1]}: launch Chromium with "
                "--force-device-scale-factor=scale(viewport)"
            )
        if len(self._frames) < 2:
            raise RuntimeError("no frames recorded")
        listing = self.workdir / "frames.txt"
        lines = []
        for (t, path), (t_next, _) in zip(self._frames, self._frames[1:]):
            lines += [f"file '{path.name}'", f"duration {max(t_next - t, 0.001):.6f}"]
        last = self._frames[-1][1].name
        hold = max(end - self._frames[-1][0], 1 / FPS)
        lines += [f"file '{last}'", f"duration {hold:.6f}", f"file '{last}'"]
        listing.write_text("\n".join(lines) + "\n")
        out.parent.mkdir(parents=True, exist_ok=True)
        _encode(["-f", "concat", "-safe", "0", "-i", str(listing)], out)
        return out

    # ---- overlay -------------------------------------------------------------

    def wait(self, seconds: float) -> None:
        # Never time.sleep: frames (and their acks) are only pumped while
        # Playwright waits.
        self.page.wait_for_timeout(seconds * 1000)

    def caption(
        self, text: Optional[str], sub: Optional[str] = None, hold: float = 0
    ) -> None:
        html = None
        if text:
            html = _inline(text) + (f"<small>{_inline(sub)}</small>" if sub else "")
        self.page.evaluate("h => window.__demo.caption(h)", html)
        self.wait(hold)

    def card(
        self,
        title: Optional[str],
        sub: str = "",
        kicker: str = "",
        code: str = "",
        hold: float = 0,
        instant: bool = False,
    ) -> None:
        html = None
        if title:
            html = (
                (f"<div class='kicker'>{kicker}</div>" if kicker else "")
                + f"<h1>{_inline(title)}</h1>"
                + (f"<p>{_inline(sub)}</p>" if sub else "")
                + (f"<code>{code}</code>" if code else "")
            )
        self.page.evaluate("([h, i]) => window.__demo.card(h, i)", [html, instant])
        self.wait(hold)

    def spot(self, selector: Optional[str], hold: float = 0) -> None:
        rect = None
        if selector:
            rect = self.page.locator(selector).first.bounding_box()
        self.page.evaluate("r => window.__demo.spot(r)", rect)
        self.wait(hold)

    def cursor(self, on: bool = True) -> None:
        self.page.evaluate("on => window.__demo.cursorShow(on)", on)

    # ---- actions -------------------------------------------------------------

    def goto(self, url: str, fade: bool = True) -> None:
        """Navigate, fading the page out and the new one in."""
        if fade and self.page.url not in ("about:blank", ""):
            self.page.evaluate("window.__demo.veil(true)")
            self.wait(0.4)
        if fade:
            self.page.evaluate("f => { window.name = f; }", WINDOW_FLAG)
        position = (
            self.page.evaluate("window.__demo ? window.__demo.cursor : null")
            if self.page.url not in ("about:blank", "")
            else None
        )
        self.page.goto(url)
        self.page.wait_for_load_state("networkidle")
        self.wait(0.2)
        if position:
            self.page.evaluate("p => window.__demo.cursorAt(p[0], p[1])", position)
        if fade:
            self.page.evaluate("window.__demo.veil(false)")
            self.wait(0.4)

    def move_to(self, selector: str, ms: int = 650) -> tuple[float, float]:
        target = self.page.locator(selector).first
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box, f"{selector} has no box"
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.cursor(True)
        self.page.evaluate(
            "([x, y, ms]) => window.__demo.cursorTo(x, y, ms)", [x, y, ms]
        )
        self.page.mouse.move(x, y)
        return x, y

    def click(
        self, selector: str, after: float = 0.6, ms: int = 650, navigates: bool = False
    ) -> None:
        """Move the cursor to ``selector`` and click it. With ``navigates``,
        fade out first and the new page in after, as ``goto`` does."""
        x, y = self.move_to(selector, ms)
        self.wait(0.08)
        self.page.evaluate("window.__demo.ripple()")
        if navigates:
            self.wait(0.15)
            self.page.evaluate("window.__demo.veil(true)")
            self.wait(0.35)
            self.page.evaluate("f => { window.name = f; }", WINDOW_FLAG)
            with self.page.expect_navigation():
                self.page.mouse.click(x, y)
            self.page.wait_for_load_state("networkidle")
            self.wait(0.2)
            self.page.evaluate("p => window.__demo.cursorAt(p[0], p[1])", [x, y])
            self.cursor(True)
            self.page.evaluate("window.__demo.veil(false)")
        else:
            self.page.mouse.click(x, y)
        self.wait(after)

    def type(self, text: str, delay: float = 0.07, after: float = 0.5) -> None:
        self.page.keyboard.type(text, delay=delay * 1000)
        self.wait(after)

    def press(self, key: str, after: float = 0.5) -> None:
        self.page.keyboard.press(key)
        self.wait(after)

    def scroll_by(self, dy: int, after: float = 0.9) -> None:
        self.page.evaluate("dy => window.scrollBy({top: dy, behavior: 'smooth'})", dy)
        self.wait(after)

    def pan(self, dx: int, after: float = 1.2) -> None:
        """Smooth-scroll the page sideways (a layout wider than the window)."""
        self.page.evaluate("dx => window.scrollBy({left: dx, behavior: 'smooth'})", dx)
        self.wait(after)

    def scroll_to(self, selector: str, offset: int = 140, after: float = 1.0) -> None:
        """Smooth-scroll so ``selector`` sits ``offset`` px below the top."""
        self.page.locator(selector).first.evaluate(
            """(el, off) => {
                const top = el.getBoundingClientRect().top + scrollY - off;
                window.scrollTo({top, behavior: 'smooth'});
            }""",
            offset,
        )
        self.wait(after)


def _inline(text: str) -> str:
    """Escape ``text`` for HTML, turning `backticks` into <code>."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


def _encode(inputs: list[str], out: Path, filters: str = "") -> None:
    vf = f"fps={FPS},format=yuv420p" + (f",{filters}" if filters else "")
    cmd = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
        "-vf", vf, "-c:v", "libx264", "-preset", "slow", "-crf", "32",
        "-tune", "stillimage", "-movflags", "+faststart", str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)


def join(clips: list[Clip], out: Path, fade: float = 0.6) -> Path:
    """Concatenate ``clips`` with an ``xfade`` transition between each pair."""
    if len(clips) == 1:
        shutil.copy(clips[0].path, out)
        return out
    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip.path)]
    graph = []
    offset = 0.0
    previous = "[0:v]"
    for i, clip in enumerate(clips[1:], start=1):
        offset += duration(clips[i - 1].path) - fade
        label = f"[v{i}]"
        graph.append(
            f"{previous}[{i}:v]xfade=transition={clip.transition}:"
            f"duration={fade}:offset={offset:.3f}{label}"
        )
        previous = label
    cmd = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
        "-filter_complex", ";".join(graph), "-map", previous,
        "-c:v", "libx264", "-preset", "slow", "-crf", "32", "-tune", "stillimage",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return out


def write_manifest(out: Path, entries: list[dict[str, Any]]) -> None:
    out.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

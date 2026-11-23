"""Another local page can run recent-data.js, but can't read it.

``recent-data.js`` is data shipped as a script, so that ``recent.html`` can
reload it from ``file://``. Any HTML file the reader opens locally (a
download, an attachment) can include it by relative path and receive what
it passes to ``window.__cclRecent``. Measured before the payload was
encrypted: a page in ``~/Downloads`` got every recent session, in Chromium
and WebKit, knowing nothing but the archive's usual place. Now it gets
ciphertext, and only ``recent.html``, which holds the key, can read it.

Run in each engine where it is installed (CI installs Chromium only):
decryption is WebCrypto's HKDF, whose limits differ by engine (Firefox
refuses PBKDF2 outputs past 256 bytes, the first design's keystream).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_code_log.html.renderer import generate_recent_html
from claude_code_log.recent import recent_data_js

pytestmark = pytest.mark.browser

TITLE = "A very particular session title"

ATTACKER = """<!doctype html><body><script>
window.got = null;
window.__cclRecent = function (p) { window.got = p; };
</script><script src="../.claude/projects/recent-data.js"></script></body>"""


def _session(i: int, title: str) -> dict[str, Any]:
    return {
        "id": f"{i:08x}-1111-4000-8000-000000000000",
        "short": f"{i:08x}",
        "project": "foldyard",
        "projectDir": "-home-u-foldyard",
        "title": title,
        "firstAt": 1_793_465_000_000,
        "lastAt": 1_793_465_000_000 + i,
        "state": "done",
        "messages": 2,
        "turns": [],
        "last": {"at": 1_793_465_000_000, "who": "tool", "text": "x" * 400},
    }


def _home(tmp_path: Path, sessions: int = 1) -> tuple[Path, Path]:
    """``~/.claude/projects`` with the recent page, and a page in
    ``~/Downloads`` that includes its data."""
    projects = tmp_path / "home" / ".claude" / "projects"
    projects.mkdir(parents=True)
    (projects / "recent.html").write_text(generate_recent_html(), encoding="utf-8")
    payload = {
        "v": 1,
        "generatedAt": 1_793_465_000_000,
        "windowDays": 3,
        "sessions": [_session(0, TITLE)]
        + [_session(i, f"filler {i}") for i in range(1, sessions)],
    }
    (projects / "recent-data.js").write_text(recent_data_js(payload), encoding="utf-8")
    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir()
    (downloads / "evil.html").write_text(ATTACKER, encoding="utf-8")
    return projects / "recent.html", downloads / "evil.html"


def _check(page: Any, recent: Path, evil: Path) -> None:
    page.goto(evil.as_uri())
    page.wait_for_function("window.got !== null")
    got = page.evaluate("window.got")
    assert isinstance(got, str), "the data file passed something readable"
    assert "particular" not in got and "foldyard" not in got

    page.goto(recent.as_uri())
    page.wait_for_function(
        "t => document.body.textContent.includes(t)", arg=TITLE, timeout=10_000
    )


def test_another_local_page_gets_only_ciphertext(page: Any, tmp_path: Path) -> None:
    _check(page, *_home(tmp_path))


def test_a_large_payload_decrypts(page: Any, tmp_path: Path) -> None:
    """More than one 8,160-byte chunk of keystream."""
    recent, evil = _home(tmp_path, sessions=60)
    assert recent.with_name("recent-data.js").stat().st_size > 3 * 8160
    _check(page, recent, evil)


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
def test_in_other_engines(playwright: Any, tmp_path: Path, engine: str) -> None:
    try:
        browser = getattr(playwright, engine).launch()
    except Exception as exc:  # not installed: CI installs Chromium only
        pytest.skip(f"{engine} not available: {exc}")
    try:
        recent, evil = _home(tmp_path, sessions=60)
        _check(browser.new_page(), recent, evil)
    finally:
        browser.close()


def test_data_it_cannot_read_is_said_on_the_page(
    page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data written under another key (the key file was replaced since the
    page was) must not look like an archive with nothing recent in it."""
    from claude_code_log import page_key

    recent, _ = _home(tmp_path)
    other = tmp_path / "other.key"
    other.write_text(bytes(range(1, 33)).hex(), encoding="ascii")
    monkeypatch.setenv(page_key.ENV_KEY_FILE, str(other))
    page_key.reset()
    try:
        recent.with_name("recent-data.js").write_text(
            recent_data_js(
                {"v": 1, "generatedAt": 1, "sessions": [_session(0, TITLE)]}
            ),
            encoding="utf-8",
        )
    finally:
        monkeypatch.undo()
        page_key.reset()
    page.goto(recent.as_uri())
    stale = page.locator("#rc-stale")
    stale.wait_for(state="visible", timeout=10_000)
    assert "can't read its data" in stale.text_content()
    assert TITLE not in page.locator("body").text_content()

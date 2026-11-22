"""A real browser can't hand another site the archive's script-shaped data.

`recent-data.js` calls `window.__cclRecent(payload)`, which is what lets
the recent page refresh from `file://`. It also means any page that
includes it with a `<script>` tag gets the payload in its own
`__cclRecent`: same-origin rules stop a page *reading* our responses, not
*running* them. `test_server.py` pins the server's half with hand-set
headers; this checks the browser really sends what the server relies on.

The attacker pages are served from a second server and opened as
`localhost` (a different site from `127.0.0.1`) or as `127.0.0.1` on
another port (the same site, like any app forwarded to loopback).
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Page

from claude_code_log.server import ArchiveServer

pytestmark = pytest.mark.browser

PAYLOAD_JS = "window.__cclRecent({ sessions: ['secret-session'] });\n"

OWN_PAGE = """<!doctype html><html><body><script>
window.received = [];
window.__cclRecent = function (p) { window.received.push(p.sessions[0]); };
</script><script src="recent-data.js"></script></body></html>"""

ATTACKER_PAGE = """<!doctype html><html><body><script>
window.received = [];
window.__cclRecent = function (p) { window.received.push(p.sessions[0]); };
</script><script src="{src}"></script></body></html>"""


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[ArchiveServer]:
    root = tmp_path / "projects"
    root.mkdir()
    (root / "recent-data.js").write_text(PAYLOAD_JS)
    (root / "recent.html").write_text(OWN_PAGE)
    with ArchiveServer(root, port=0) as server:
        yield server


@pytest.fixture
def attacker_root(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    root = tmp_path / "attacker"
    root.mkdir()
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_own_page_still_gets_its_data(page: Page, archive: ArchiveServer) -> None:
    page.goto(f"{archive.url}/recent.html")
    page.wait_for_function("window.received.length > 0")
    assert page.evaluate("window.received") == ["secret-session"]


@pytest.mark.parametrize("attacker_host", ["localhost", "127.0.0.1"])
def test_another_sites_page_cannot_include_the_data(
    page: Page,
    archive: ArchiveServer,
    attacker_root: tuple[Path, int],
    attacker_host: str,
) -> None:
    root, port = attacker_root
    (root / "evil.html").write_text(
        ATTACKER_PAGE.replace("{src}", f"{archive.url}/recent-data.js")
    )
    # Chromium may drop the refusal before Playwright sees a response (a
    # JSON body where a script was expected), so count the request itself:
    # it must have been sent, or this test would pass without a server.
    requested: list[str] = []
    statuses: list[int] = []
    page.on(
        "request",
        lambda r: requested.append(r.url) if "recent-data" in r.url else None,
    )
    page.on(
        "response",
        lambda r: statuses.append(r.status) if "recent-data" in r.url else None,
    )

    page.goto(f"http://{attacker_host}:{port}/evil.html")
    page.wait_for_load_state("load")

    assert len(requested) == 1
    assert 200 not in statuses
    assert page.evaluate("window.received") == []

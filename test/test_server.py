"""Tests for the local archive server.

These use a real socket on an ephemeral port rather than a fake transport,
because the behaviours worth pinning here are HTTP-level: Host validation,
path traversal, conditional GET, and not dying on a client disconnect.
"""

from __future__ import annotations

import http.server
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

import claude_code_log.server as server_module
from claude_code_log.server import REVISION_HEADER, ArchiveServer


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """A miniature projects directory, with a secret file *outside* it."""
    root = tmp_path / "projects"
    root.mkdir()
    (root / "index.html").write_text("<html><body>index</body></html>")
    project = root / "-Users-someone-project"
    project.mkdir()
    (project / "session-abc123.html").write_text("<html><body>session</body></html>")
    # Sibling of the served root: reachable only by escaping it.
    (tmp_path / "secret-outside.txt").write_text("should never be served")
    return root


@pytest.fixture
def server(archive: Path):
    with ArchiveServer(archive, port=0) as srv:
        yield srv


def _get(
    url: str, headers: Optional[dict[str, str]] = None
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_serves_a_static_page(server: ArchiveServer) -> None:
    status, body, _ = _get(f"{server.url}/index.html")
    assert status == 200
    assert b"index" in body


def test_serves_a_session_page_in_a_project_dir(server: ArchiveServer) -> None:
    status, body, _ = _get(f"{server.url}/-Users-someone-project/session-abc123.html")
    assert status == 200
    assert b"session" in body


def test_serves_referenced_avif_images_as_images(
    server: ArchiveServer, archive: Path
) -> None:
    # Python's mimetypes only knows .avif from 3.11; the handler maps it.
    images = archive / "-Users-someone-project" / "images"
    images.mkdir()
    (images / "image_0123456789abcdef.avif").write_bytes(b"\x00\x00\x00\x1cftypavif")
    status, _, headers = _get(
        f"{server.url}/-Users-someone-project/images/image_0123456789abcdef.avif"
    )
    assert status == 200
    assert headers["Content-type"] == "image/avif"


def test_query_string_on_a_static_path_is_ignored(server: ArchiveServer) -> None:
    """Deep links carry ?uuid=&q= — the file must still resolve."""
    status, body, _ = _get(
        f"{server.url}/-Users-someone-project/session-abc123.html?uuid=x&q=foo"
    )
    assert status == 200
    assert b"session" in body


def test_api_ping(server: ArchiveServer) -> None:
    import json

    status, body, headers = _get(f"{server.url}/api/ping")
    assert status == 200
    payload: dict[str, Any] = json.loads(body)
    assert payload["ok"] is True
    assert payload["version"]
    assert headers.get("Cache-Control") == "no-store"


def test_unknown_api_endpoint_is_404_json_not_a_file_lookup(
    server: ArchiveServer,
) -> None:
    import json

    status, body, _ = _get(f"{server.url}/api/nope")
    assert status == 404
    assert "error" in json.loads(body)


def test_api_prefix_is_reserved_from_the_filesystem(archive: Path) -> None:
    """A project directory named `api` must not shadow the API."""
    api_dir = archive / "api"
    api_dir.mkdir()
    (api_dir / "ping").write_text("this is a file, not the endpoint")
    import json

    with ArchiveServer(archive, port=0) as srv:
        status, body, _ = _get(f"{srv.url}/api/ping")
        assert status == 200
        assert json.loads(body)["ok"] is True


def _raw_get(host: str, port: int, raw_path: str) -> bytes:
    """Send a request with the path exactly as given.

    `urllib` normalises `../` client-side before it ever reaches the wire,
    so a traversal test that goes through it proves nothing about the
    server. This puts the literal path on the socket.
    """
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            f"GET {raw_path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize(
    "attempt",
    [
        "/../secret-outside.txt",
        "/../../etc/passwd",
        "/..%2f..%2fetc%2fpasswd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/....//....//etc/passwd",
        "/-Users-someone-project/../../secret-outside.txt",
    ],
)
def test_path_traversal_is_refused(server: ArchiveServer, attempt: str) -> None:
    response = _raw_get(server.host, server.port, attempt)
    assert b"should never be served" not in response
    assert b"root:" not in response
    status_line = response.split(b"\r\n", 1)[0]
    assert b"200" not in status_line, status_line


def test_host_header_must_be_loopback(server: ArchiveServer) -> None:
    """DNS rebinding: a page on the web can point a name at 127.0.0.1.

    Without this check it could then read the whole transcript archive
    same-origin. The read side is the sensitive side, so this holds before
    any write endpoint exists.
    """
    status, _, _ = _get(
        f"{server.url}/index.html", headers={"Host": "attacker.example.com"}
    )
    assert status == 421


def test_host_header_allows_localhost_with_port(server: ArchiveServer) -> None:
    status, _, _ = _get(
        f"{server.url}/index.html", headers={"Host": f"localhost:{server.port}"}
    )
    assert status == 200


def test_host_check_also_guards_the_api(server: ArchiveServer) -> None:
    status, _, _ = _get(
        f"{server.url}/api/ping", headers={"Host": "attacker.example.com"}
    )
    assert status == 421


def _fetch_meta(site: str, mode: str, dest: str) -> dict[str, str]:
    """The `Sec-Fetch-*` headers a browser attaches to a request."""
    return {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}


class TestCrossSiteRequests:
    """Other sites' pages can't make the browser fetch the archive for them.

    The Host check stops DNS rebinding, but not a page that simply names
    the server: `<script src=".../recent-data.js">` runs the file in the
    including page, whose own `window.__cclRecent` then receives every
    recent session. Same-origin rules protect a response only from being
    *read*, and a script is executed, not read. Browsers say where a
    request comes from in `Sec-Fetch-Site`, so the server refuses the ones
    another site's page made.
    """

    @pytest.mark.parametrize(
        ("site", "mode", "dest", "path"),
        [
            # The leak: another site includes the recent page's data file.
            ("cross-site", "no-cors", "script", "/index.html"),
            # A different port on the same machine is the same *site*:
            # a box's web app forwarded to 127.0.0.1:3000 is one of these.
            ("same-site", "no-cors", "script", "/index.html"),
            ("cross-site", "cors", "empty", "/api/ping"),
            ("cross-site", "no-cors", "image", "/index.html"),
            # Framing another site's page in would let it dress ours up.
            ("cross-site", "navigate", "iframe", "/index.html"),
            ("same-site", "navigate", "iframe", "/index.html"),
        ],
    )
    def test_another_sites_request_is_refused(
        self, server: ArchiveServer, site: str, mode: str, dest: str, path: str
    ) -> None:
        status, body, _ = _get(
            f"{server.url}{path}", headers=_fetch_meta(site, mode, dest)
        )
        assert status == 403
        assert b"index" not in body

    def test_another_sites_head_is_refused(self, server: ArchiveServer) -> None:
        request = urllib.request.Request(
            f"{server.url}/index.html",
            method="HEAD",
            headers=_fetch_meta("cross-site", "no-cors", "script"),
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 403

    @pytest.mark.parametrize(
        ("site", "mode", "dest"),
        [
            # The server's own pages loading their data, images, and API.
            ("same-origin", "no-cors", "script"),
            ("same-origin", "cors", "empty"),
            # A typed URL or a bookmark.
            ("none", "navigate", "document"),
            # Following a link from elsewhere into the archive: the linking
            # page can't read what opens, so there is nothing to refuse.
            ("cross-site", "navigate", "document"),
            ("same-site", "navigate", "document"),
        ],
    )
    def test_own_and_top_level_requests_are_served(
        self, server: ArchiveServer, site: str, mode: str, dest: str
    ) -> None:
        status, _, _ = _get(
            f"{server.url}/index.html", headers=_fetch_meta(site, mode, dest)
        )
        assert status == 200

    def test_a_request_without_fetch_metadata_is_served(
        self, server: ArchiveServer
    ) -> None:
        # curl, scripts, and browsers too old to send it.
        status, _, _ = _get(f"{server.url}/index.html")
        assert status == 200


class TestExtraHosts:
    """Names besides loopback the server answers to, for a reverse proxy.

    `tailscale serve` forwards to loopback but keeps the browser's `Host`
    (the machine's `*.ts.net` name), so without an opt-in every proxied
    request is a 421. Allowing one named host must not loosen the check
    for anything else.
    """

    TAILNET = "box.tailnet-1234.ts.net"

    @pytest.fixture
    def proxied(self, archive: Path):
        with ArchiveServer(archive, port=0, extra_hosts=[self.TAILNET]) as srv:
            yield srv

    @pytest.mark.parametrize(
        "host",
        [TAILNET, f"{TAILNET}:443", f"{TAILNET}:8443", TAILNET.upper()],
    )
    def test_an_allowed_host_is_served(self, proxied: ArchiveServer, host: str) -> None:
        status, body, _ = _get(f"{proxied.url}/index.html", headers={"Host": host})
        assert status == 200
        assert b"index" in body

    def test_an_allowed_host_reaches_the_api(self, proxied: ArchiveServer) -> None:
        status, _, _ = _get(f"{proxied.url}/api/ping", headers={"Host": self.TAILNET})
        assert status == 200

    def test_an_allowed_host_can_head(self, proxied: ArchiveServer) -> None:
        # live_update.js polls with HEAD, so the proxied page needs it too.
        request = urllib.request.Request(
            f"{proxied.url}/index.html", method="HEAD", headers={"Host": self.TAILNET}
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert response.headers[REVISION_HEADER]

    def test_other_hosts_are_still_refused(self, proxied: ArchiveServer) -> None:
        status, _, _ = _get(
            f"{proxied.url}/index.html", headers={"Host": "attacker.example.com"}
        )
        assert status == 421

    def test_a_suffix_match_is_not_enough(self, proxied: ArchiveServer) -> None:
        status, _, _ = _get(
            f"{proxied.url}/index.html",
            headers={"Host": f"evil-{self.TAILNET}"},
        )
        assert status == 421

    def test_loopback_still_works(self, proxied: ArchiveServer) -> None:
        status, _, _ = _get(f"{proxied.url}/index.html")
        assert status == 200

    def test_an_allowed_host_given_in_mixed_case_matches(self, archive: Path) -> None:
        with ArchiveServer(archive, port=0, extra_hosts=["Box.Example.TS.net"]) as srv:
            status, _, _ = _get(
                f"{srv.url}/index.html", headers={"Host": "box.example.ts.net"}
            )
        assert status == 200


def test_conditional_get_returns_304(server: ArchiveServer) -> None:
    """2.26 GB of pages in a real archive — 304s are worth having."""
    status, _, headers = _get(f"{server.url}/index.html")
    assert status == 200
    last_modified = headers["Last-Modified"]

    status, body, _ = _get(
        f"{server.url}/index.html", headers={"If-Modified-Since": last_modified}
    )
    assert status == 304
    assert body == b""


def test_a_same_size_rewrite_in_the_same_second_changes_the_revision(
    server: ArchiveServer,
    archive: Path,
) -> None:
    """The header the live page polls on must follow the *bytes*.

    `Last-Modified` is second-granular and `Content-Length` cannot see an
    edit that keeps the size, so a re-render where a counter or a status
    word keeps its width is invisible to both — and watch mode rewrites a
    few hundred ms apart.

    The two mtimes are set explicitly, half a second apart inside one
    whole second, so this is exactly a same-second rewrite and not a
    timing gamble. Dating them in the past also means the first digest is
    genuinely cached (the cache only keeps a settled file's), so the
    second response is pinned against reusing it.
    """
    page = archive / "-Users-someone-project" / "session-abc123.html"
    url = f"{server.url}/-Users-someone-project/session-abc123.html"

    second_ns = 1_700_000_000_000_000_000
    os.utime(page, ns=(second_ns, second_ns))
    original = os.stat(page)

    status, _, before = _get(url)
    assert status == 200
    first = before[REVISION_HEADER]

    page.write_text("<html><body>SESSION</body></html>")
    assert page.stat().st_size == original.st_size
    os.utime(page, ns=(second_ns + 500_000_000, second_ns + 500_000_000))

    status, body, after = _get(url)
    assert status == 200
    assert b"SESSION" in body
    assert after["Last-Modified"] == before["Last-Modified"]
    assert after["Content-Length"] == before["Content-Length"]
    assert after[REVISION_HEADER] != first


def test_the_revision_is_stable_while_the_file_is(server: ArchiveServer) -> None:
    """An unchanged file must not look changed, or every poll re-fetches."""
    url = f"{server.url}/index.html"
    _, _, first = _get(url)
    _, _, second = _get(url)
    assert first[REVISION_HEADER] == second[REVISION_HEADER]


def test_head_carries_the_revision(server: ArchiveServer) -> None:
    """The live page polls with HEAD; the header has to be on that reply."""
    request = urllib.request.Request(f"{server.url}/index.html", method="HEAD")
    with urllib.request.urlopen(request) as response:
        assert response.status == 200
        assert response.headers[REVISION_HEADER]


def test_the_api_carries_no_revision(server: ArchiveServer) -> None:
    """It describes a file response; a JSON payload has none."""
    _, _, headers = _get(f"{server.url}/api/ping")
    assert REVISION_HEADER not in headers


def test_client_disconnect_does_not_kill_the_server(
    server: ArchiveServer, archive: Path
) -> None:
    """Navigating away mid-transfer is routine with multi-MB pages.

    The stock handler answers it with a BrokenPipeError traceback; this
    pins that the server stays healthy and quiet.
    """
    big = archive / "big.html"
    big.write_text("x" * 8_000_000)

    # Send a request, read a little, then hang up mid-body.
    host, port = server.host, server.port
    with socket.create_connection((host, port)) as sock:
        sock.sendall(
            f"GET /big.html HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        )
        sock.recv(1024)

    # The server must still answer.
    status, body, _ = _get(f"{server.url}/index.html")
    assert status == 200
    assert b"index" in body


SESSION_URL_PATH = "/-Users-someone-project/session-abc123.html"


def _open_failing(
    monkeypatch: pytest.MonkeyPatch, target: Path, failures: Optional[int]
) -> list[str]:
    """Make every `open` of ``target`` by the server raise PermissionError.

    ``failures`` opens fail (all of them when None), then they succeed —
    what an `open` racing `atomic_write_text`'s `os.replace` sees on
    Windows. Patched where the stock handler opens the file as well as
    where ours does, so the race is simulated the same on either side of
    the fix. Returns the outcome of each attempt, in order.
    """
    real_open = open
    outcomes: list[str] = []

    def racing_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(file) != str(target):
            return real_open(file, *args, **kwargs)
        if failures is None or len(outcomes) < failures:
            outcomes.append("denied")
            raise PermissionError(13, "The process cannot access the file")
        outcomes.append("opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(http.server, "open", racing_open, raising=False)
    monkeypatch.setattr(server_module, "open", racing_open, raising=False)
    # The Windows count, so the retry runs whatever the platform.
    monkeypatch.setattr("claude_code_log.utils._SHARING_RETRY_ATTEMPTS", 10)
    monkeypatch.setattr("claude_code_log.utils._SHARING_RETRY_BACKOFF_S", 0.001)
    return outcomes


def test_a_page_mid_swap_is_served_not_404(
    server: ArchiveServer, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live page's poll must not see a 404 while its file is replaced.

    On Windows an `open` racing the writer's `os.replace` can raise
    PermissionError, which the stock handler turns into a 404.
    """
    page = archive / "-Users-someone-project" / "session-abc123.html"
    outcomes = _open_failing(monkeypatch, page.resolve(), failures=3)

    status, body, headers = _get(f"{server.url}{SESSION_URL_PATH}")

    assert status == 200
    assert b"session" in body
    assert headers[REVISION_HEADER]
    assert outcomes[:4] == ["denied", "denied", "denied", "opened"], (
        "the open was not retried past the swap"
    )


def test_a_file_that_stays_locked_is_still_404_in_bounded_time(
    server: ArchiveServer, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = archive / "-Users-someone-project" / "session-abc123.html"
    outcomes = _open_failing(monkeypatch, page.resolve(), failures=None)

    started = time.monotonic()
    status, _, _ = _get(f"{server.url}{SESSION_URL_PATH}")

    assert status == 404
    assert time.monotonic() - started < 5
    assert outcomes.count("denied") >= 10


def test_a_missing_file_is_404_without_retrying(
    server: ArchiveServer, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = archive / "-Users-someone-project" / "session-gone.html"
    attempts: list[str] = []
    real_open = open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(file) == str(missing.resolve()):
            attempts.append("open")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(http.server, "open", counting_open, raising=False)
    monkeypatch.setattr(server_module, "open", counting_open, raising=False)

    status, _, _ = _get(f"{server.url}/-Users-someone-project/session-gone.html")

    assert status == 404
    # Ours, then the stock handler's: one each, neither retried.
    assert len(attempts) <= 2


def test_server_reports_its_bound_port(archive: Path) -> None:
    with ArchiveServer(archive, port=0) as srv:
        assert srv.port > 0
        assert srv.url == f"http://127.0.0.1:{srv.port}"


def test_api_500_does_not_echo_the_exception(
    archive: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """An exception's text can carry local paths; it goes to stderr only."""

    def boom(_params: dict[str, str]) -> Any:
        raise RuntimeError("/home/someone/private/path")

    with ArchiveServer(archive, port=0, api_routes={"/api/boom": boom}) as srv:
        status, body, _ = _get(f"{srv.url}/api/boom")
    assert status == 500
    assert b"private" not in body
    err = capfd.readouterr().err
    assert "RuntimeError: /home/someone/private/path" in err
    assert "Traceback" in err

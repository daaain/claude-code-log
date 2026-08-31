"""Tests for the local archive server.

These use a real socket on an ephemeral port rather than a fake transport,
because the behaviours worth pinning here are HTTP-level: Host validation,
path traversal, conditional GET, and not dying on a client disconnect.
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

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


def test_server_reports_its_bound_port(archive: Path) -> None:
    with ArchiveServer(archive, port=0) as srv:
        assert srv.port > 0
        assert srv.url == f"http://127.0.0.1:{srv.port}"

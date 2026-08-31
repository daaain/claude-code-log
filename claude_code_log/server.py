"""A local, single-user web server for the generated archive.

Why this exists: the generated HTML is canonical and keeps working from
`file://`, but two things are impossible there — reading the SQLite cache
(so full-archive search) and persisting anything (annotations, later). Both
need an origin, so this serves the projects directory over loopback and
hangs a small JSON API off `/api/`.

Design notes, in case the shape looks arbitrary:

* **`SimpleHTTPRequestHandler` does the static serving.** 99% of requests are
  static files and it already handles them correctly: hardened path
  translation, `Last-Modified`/304, streamed transfers. Hand-rolling a
  "safe path resolver" is exactly where directory-traversal bugs come from.
* **Threading is required.** Session pages reach 27 MB; without it an
  `/api/search` landing during a page transfer waits behind it (measured
  44.7 ms vs 5.9 ms median).
* **The `Host` header is validated.** The stock handler ignores it entirely,
  which is a DNS-rebinding hole — and the read side already exposes every
  transcript in the archive, so this matters before any write endpoint
  exists, not after.
* **The search core is HTTP-free.** Everything interesting lives in
  `search.py` as plain functions; this module is the adapter.
* **File responses carry `X-Content-Revision`.** The live page asks "did
  this change?" with a HEAD, and the stock validators cannot always
  answer: `Last-Modified` has one-second granularity and `Content-Length`
  is blind to an edit that keeps the size. See `_content_revision`.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

from .cache import get_library_version

# Requests under this prefix are the JSON API and never hit the filesystem.
# Reserved so a project directory literally named `api` can't shadow it.
API_PREFIX = "/api/"

# The header the live page adds to its change comparison (see
# `_content_revision` and `live_update.js`). Deliberately *not* `ETag`:
# the stock `send_head` skips its `If-Modified-Since` check whenever the
# request carries an `If-None-Match`, and it never evaluates one — so
# advertising an ETag would make browsers stop getting 304s on the
# multi-MB pages that make 304s worth having.
REVISION_HEADER = "X-Content-Revision"

# How long a file's mtime must have been in the past before a cached
# digest for it can be trusted. A write landing after we hashed can only
# reuse the same (mtime_ns, size) key if the filesystem's timestamp
# resolution is coarse enough to give it the same mtime — so once the
# recorded mtime is a full second old, no later write can hide behind it,
# whatever the resolution. Below that, re-hash: an actively-written page
# is exactly the case this header exists for.
_REVISION_SETTLE_NS = 1_000_000_000


class ArchiveHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static file serving for the projects dir, plus the `/api/` routes."""

    # Set by the factory in `build_handler`.
    api_routes: dict[str, Callable[[dict[str, str]], Any]] = {}
    allowed_hosts: frozenset[str] = frozenset()
    server_version = f"claude-code-log/{get_library_version()}"
    sys_version = ""

    # Digest of the file this request is answering with, computed in
    # `send_head` and emitted by `end_headers`. `None` for everything
    # that is not a file response (the API, errors, directory listings).
    _revision: Optional[str] = None

    # (path, mtime_ns, size) -> digest, shared by every request. Written
    # from several server threads: dict get/set are atomic, and the worst
    # a race can do is hash the same file twice.
    _revision_cache: dict[tuple[str, int, int], str] = {}

    # ---- security -------------------------------------------------------

    def _host_is_allowed(self) -> bool:
        """Reject requests that don't address us as loopback.

        A page on the open web can point a hostname at 127.0.0.1 (DNS
        rebinding) and then read our responses same-origin. Checking `Host`
        blocks that; there is no legitimate request with another value here.
        """
        host = self.headers.get("Host", "")
        # Strip the port: loopback on any port is us.
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname.lower() in self.allowed_hosts

    # ---- plumbing -------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the per-request access log (it goes to stderr by default)."""
        return

    def handle_one_request(self) -> None:
        """Swallow client disconnects.

        Navigating away mid-transfer is routine with multi-MB pages, and the
        stock handler answers it with a full BrokenPipeError traceback on
        stderr. Nothing is wrong when that happens, so it should not look
        like it.
        """
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---- responses ------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Search results reflect a mutable cache; never let them be reused.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- content revision -----------------------------------------------

    def _content_revision(self, path: str) -> Optional[str]:
        """A validator derived from the bytes, not from the metadata.

        `live_update.js` polls a HEAD of its own URL and re-fetches when
        the response's identity moves. `Last-Modified` alone misses two
        conversions inside one second (HTTP dates are second-granular),
        and adding `Content-Length` still misses a rewrite that changes
        content without changing size — a re-render where a counter, a
        status word or a timestamp keeps its width. Watch mode produces
        rewrites a few hundred ms apart, so both gaps are reachable.

        Hashing the file closes them for any filesystem. It is not free
        on a 27 MB page, so a settled file (see `_REVISION_SETTLE_NS`)
        answers from the cache and only a file being written right now
        is re-read — which is the case that has to be exact.
        """
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None

        key = (path, st.st_mtime_ns, st.st_size)
        settled = time.time_ns() - st.st_mtime_ns > _REVISION_SETTLE_NS
        if settled:
            cached = self._revision_cache.get(key)
            if cached is not None:
                return cached

        digest = hashlib.blake2b(digest_size=16)
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return None
        revision = digest.hexdigest()

        if settled:
            # One entry per served file; a long-running server over a
            # whole archive would otherwise accumulate them forever.
            if len(self._revision_cache) > 256:
                self._revision_cache.clear()
            self._revision_cache[key] = revision
        return revision

    def send_head(self) -> Optional[BinaryIO]:
        self._revision = None
        path = self.translate_path(self.path)
        if not os.path.isdir(path):
            self._revision = self._content_revision(path)
        return super().send_head()

    def end_headers(self) -> None:
        if self._revision is not None:
            self.send_header(REVISION_HEADER, self._revision)
        super().end_headers()

    def _split_query(self) -> tuple[str, dict[str, str]]:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        return parsed.path, params

    # ---- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if not self._host_is_allowed():
            self._send_json({"error": "invalid Host header"}, status=421)
            return

        path, params = self._split_query()
        if path.startswith(API_PREFIX) or path == API_PREFIX.rstrip("/"):
            handler = self.api_routes.get(path)
            if handler is None:
                self._send_json({"error": f"no such endpoint: {path}"}, status=404)
                return
            try:
                self._send_json(handler(params))
            except ValueError as exc:
                # Bad user input (unparseable filter, malformed query).
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:  # pragma: no cover - defensive
                self._send_json({"error": repr(exc)}, status=500)
            return

        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._host_is_allowed():
            self.send_response(421)
            self.end_headers()
            return
        super().do_HEAD()


def build_handler(
    directory: Path,
    api_routes: dict[str, Callable[[dict[str, str]], Any]],
    allowed_hosts: frozenset[str],
) -> Callable[..., http.server.BaseHTTPRequestHandler]:
    """Bind a handler class to one directory and route table.

    `http.server` instantiates the handler per request, so per-server config
    is carried on a fresh subclass (routes, hosts) and `directory` through
    the documented `functools.partial` idiom.
    """

    class _Handler(ArchiveHTTPRequestHandler):
        pass

    _Handler.api_routes = api_routes
    _Handler.allowed_hosts = allowed_hosts
    return functools.partial(_Handler, directory=str(directory))


class ArchiveServer:
    """A running server, usable as a context manager (handy in tests)."""

    def __init__(
        self,
        directory: Path,
        api_routes: Optional[dict[str, Callable[[dict[str, str]], Any]]] = None,
        host: str = "127.0.0.1",
        port: int = 8010,
    ) -> None:
        self.directory = directory
        self.host = host
        routes: dict[str, Callable[[dict[str, str]], Any]] = {
            "/api/ping": lambda _params: {
                "ok": True,
                "version": get_library_version(),
            },
        }
        routes.update(api_routes or {})
        allowed = frozenset({"127.0.0.1", "localhost", "[::1]", "::1", ""})
        self._httpd = http.server.ThreadingHTTPServer(
            (host, port), build_handler(directory, routes, allowed)
        )
        # daemon_threads: don't let an in-flight 27 MB transfer hold up exit.
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ArchiveServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

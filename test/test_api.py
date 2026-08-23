"""Tests for the JSON search API.

Covers the parameter plumbing and the error contract. The interesting
performance property — that `/api/search` doesn't accidentally do expensive
work per request — is pinned by `test_search_does_not_scan_the_messages_table`.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.api import MAX_LIMIT, SearchApi
from claude_code_log.search import SEARCH_FIELDS, ensure_index
from claude_code_log.server import ArchiveServer


def _make_cache(db_path: Path) -> sqlite3.Connection:
    from claude_code_log.migrations.runner import run_migrations

    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    for pid, name in ((1, "-home-u-alpha"), (2, "-home-u-beta")):
        conn.execute(
            "INSERT INTO projects(id, project_path, version, cache_created, "
            "last_updated, total_message_count, latest_timestamp) "
            "VALUES(?, ?, '1', 'x', 'x', 2, ?)",
            (pid, f"/home/u/.claude/projects/{name}", f"2026-0{pid}-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO cached_files(id, project_id, file_name, file_path, "
            "source_mtime, cached_mtime, message_count) "
            "VALUES(?, ?, ?, ?, 0, 0, 2)",
            (pid, pid, f"f{pid}.jsonl", f"/tmp/f{pid}.jsonl"),
        )
    conn.commit()
    return conn


def _add(
    conn: sqlite3.Connection,
    message_id: int,
    project_id: int,
    text: str,
    *,
    session_id: str,
    uuid: str,
    entry_type: str = "assistant",
) -> None:
    entry: dict[str, Any] = {
        "type": entry_type,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    conn.execute(
        "INSERT INTO messages(id, project_id, file_id, type, timestamp, session_id, "
        "_uuid, content) VALUES(?, ?, ?, ?, '2026-01-01T00:00:00Z', ?, ?, ?)",
        (
            message_id,
            project_id,
            project_id,
            entry_type,
            session_id,
            uuid,
            zlib.compress(json.dumps(entry).encode("utf-8")),
        ),
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cache.db"
    conn = _make_cache(path)
    _add(conn, 1, 1, "alpha mentions pydantic", session_id="s-a", uuid="u-1")
    _add(conn, 2, 1, "alpha mentions sqlite", session_id="s-a", uuid="u-2")
    _add(conn, 3, 2, "beta mentions pydantic", session_id="s-b", uuid="u-3")
    ensure_index(conn)
    conn.close()
    return path


@pytest.fixture
def api(db_path: Path) -> SearchApi:
    return SearchApi(db_path)


@pytest.fixture
def client(api: SearchApi, tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "index.html").write_text("<html>index</html>")
    with ArchiveServer(root, api_routes=api.routes(), port=0) as server:

        def get(path: str, **params: str) -> tuple[int, dict[str, Any]]:
            url = f"{server.url}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            try:
                with urllib.request.urlopen(url) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        yield get


# --- ping / projects ------------------------------------------------------


def test_ping_reports_index_state(client: Any) -> None:
    status, body = client("/api/ping")
    assert status == 200
    assert body["ok"] is True
    assert body["index"]["ready"] is True
    assert body["index"]["indexed_messages"] == 3
    assert body["fields"]["available"] == list(SEARCH_FIELDS)
    assert "tool_result" not in body["fields"]["default"]


def test_projects_lists_slugs_not_absolute_paths(client: Any) -> None:
    """A moved archive must still resolve — only the basename is portable."""
    status, body = client("/api/projects")
    assert status == 200
    slugs = {p["slug"] for p in body["projects"]}
    assert slugs == {"-home-u-alpha", "-home-u-beta"}
    for project in body["projects"]:
        assert "/" not in project["slug"]


# --- search ---------------------------------------------------------------


def test_search_across_all_projects(client: Any) -> None:
    status, body = client("/api/search", q="pydantic")
    assert status == 200
    assert body["total"] == 2
    assert len(body["results"]) == 2


def test_search_filtered_by_project_slug(client: Any) -> None:
    _, body = client("/api/search", q="pydantic", project="-home-u-beta")
    assert len(body["results"]) == 1
    assert body["results"][0]["project"] == "-home-u-beta"
    assert body["project"] == "-home-u-beta"


def test_search_filtered_by_session(client: Any) -> None:
    _, body = client("/api/search", q="pydantic", session="s-b")
    assert [r["session_id"] for r in body["results"]] == ["s-b"]


def test_results_carry_a_deep_link(client: Any) -> None:
    _, body = client("/api/search", q="sqlite")
    link = body["results"][0]["link"]
    assert link.startswith("-home-u-alpha/session-s-a.html?")
    assert "uuid=u-2" in link
    assert "q=sqlite" in link


def test_empty_query_returns_empty_without_erroring(client: Any) -> None:
    status, body = client("/api/search", q="   ")
    assert status == 200
    assert body["results"] == []


def test_unparseable_query_is_not_an_error(client: Any) -> None:
    """`vis-timeline` is invalid FTS syntax; escaping must absorb it."""
    status, body = client("/api/search", q="vis-timeline")
    assert status == 200
    assert body["results"] == []


def test_fields_parameter_widens_the_search(db_path: Path, client: Any) -> None:
    conn = sqlite3.connect(db_path)
    entry: dict[str, Any] = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "content": "rare_token in output"}]
        },
    }
    conn.execute(
        "INSERT INTO messages(id, project_id, file_id, type, timestamp, session_id, "
        "_uuid, content) VALUES(9, 1, 1, 'user', '2026-01-01T00:00:00Z', 's-a', 'u-9', ?)",
        (zlib.compress(json.dumps(entry).encode("utf-8")),),
    )
    conn.commit()
    ensure_index(conn, rebuild=True)
    conn.close()

    _, default_body = client("/api/search", q="rare_token")
    assert default_body["results"] == []

    _, all_body = client("/api/search", q="rare_token", fields="all")
    assert len(all_body["results"]) == 1
    assert all_body["results"][0]["field"] == "tool_result"


def test_limit_is_capped(client: Any) -> None:
    _, body = client("/api/search", q="pydantic", limit="99999")
    assert body["limit"] == MAX_LIMIT


def test_offset_pages_through_results(client: Any) -> None:
    _, first = client("/api/search", q="pydantic", limit="1")
    _, second = client("/api/search", q="pydantic", limit="1", offset="1")
    assert first["results"][0]["message_id"] != second["results"][0]["message_id"]


# --- error contract -------------------------------------------------------


@pytest.mark.parametrize(
    "params,fragment",
    [
        ({"q": "x", "project": "no-such-project"}, "unknown project"),
        ({"q": "x", "limit": "abc"}, "must be an integer"),
        ({"q": "x", "fields": "bogus"}, "Unknown search field"),
        ({"q": "x", "fields": "text,+meta"}, "mixes absolute"),
    ],
)
def test_bad_input_is_400_with_a_message(
    client: Any, params: dict[str, str], fragment: str
) -> None:
    status, body = client("/api/search", **params)
    assert status == 400
    assert fragment in body["error"]


def test_search_without_an_index_is_a_clean_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    _make_cache(path).close()
    api = SearchApi(path)
    root = tmp_path / "projects"
    root.mkdir()
    with ArchiveServer(root, api_routes=api.routes(), port=0) as server:
        try:
            urllib.request.urlopen(f"{server.url}/api/search?q=anything")
            raise AssertionError("expected an error response")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "not available" in json.loads(exc.read())["error"]


# --- the performance contract --------------------------------------------


def test_search_does_not_scan_the_messages_table(db_path: Path) -> None:
    """Regression guard for a 1000x slowdown that no test would otherwise see.

    `index_status` originally counted rows in `messages`. Because the content
    BLOB is stored inline, that full scan reads ~930 MB on a real archive and
    made every `/api/search` take 2.6 s instead of 2.4 ms. Counting the
    statements a search issues is the cheap way to keep it honest.
    """
    api = SearchApi(db_path)
    conn = api.connection()
    scans: list[str] = []

    def trace(statement: str) -> None:
        normalised = " ".join(statement.split()).lower()
        if "from messages" in normalised and "where" not in normalised:
            scans.append(statement)

    conn.set_trace_callback(trace)
    api.search({"q": "pydantic"})
    conn.set_trace_callback(None)
    assert scans == [], f"unfiltered scan of messages: {scans}"

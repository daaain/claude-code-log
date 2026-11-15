"""Transcript and directory values placed in HTML attributes stay inert.

Text content is escaped everywhere, but an attribute has two more ways to
go wrong: a quote that ends the attribute early (and lets the value add an
``onmouseover=``), and a link whose first path segment reads as a URL
scheme (a project directory named ``javascript:alert(1)``). These tests
cover the server-side halves; ``test_xss_attributes_browser.py`` checks
the pages' search scripts in Chromium.
"""

from __future__ import annotations

import os

import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.html import format_todowrite_input
from claude_code_log.models import TodoWriteInput, TodoWriteItem
from claude_code_log.recent import build_recent_payload
from claude_code_log.search import build_link

HOSTILE_SESSION = 'x" onmouseover="window.__pwn=1" y="'
HOSTILE_SESSION_SQ = "x' onmouseover='window.__pwn=1' y='"
SCHEME_PROJECT = "javascript:alert(1)"

# Session ids with quotes and directory names with colons can't be file
# names on Windows, so the end-to-end tests run where they can exist.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="quotes and colons in file names"
)


class _Anchors(HTMLParser):
    """Every element's attributes, as the browser's parser would see them."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {k: v or "" for k, v in attrs}))


def _elements(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = _Anchors()
    parser.feed(html)
    return parser.elements


# ---- TodoWrite status / priority ---------------------------------------


class TestTodoWriteClasses:
    def test_hostile_status_cannot_add_attributes(self) -> None:
        html = format_todowrite_input(
            TodoWriteInput(
                todos=[
                    TodoWriteItem(
                        id="1",
                        content="task",
                        status='pending" onmouseover="alert(1)" x="',
                        priority="medium' onclick='alert(2)' y='",
                    )
                ]
            )
        )
        elements = _elements(html)
        assert all(
            not name.startswith("on") for _, attrs in elements for name in attrs
        ), elements
        assert "onmouseover" not in html
        assert "onclick" not in html

    def test_unknown_values_fall_back_to_no_class(self) -> None:
        html = format_todowrite_input(
            TodoWriteInput(
                todos=[TodoWriteItem(content="t", status="weird", priority="urgent")]
            )
        )
        assert '<div class="todo-item">' in html

    @pytest.mark.parametrize("status", ["pending", "in_progress", "completed"])
    @pytest.mark.parametrize("priority", ["high", "medium", "low"])
    def test_known_values_keep_their_classes(self, status: str, priority: str) -> None:
        html = format_todowrite_input(
            TodoWriteInput(
                todos=[TodoWriteItem(content="t", status=status, priority=priority)]
            )
        )
        assert f'<div class="todo-item {status} {priority}">' in html


# ---- relative links -----------------------------------------------------


class TestRelativeHref:
    @pytest.mark.parametrize(
        "path,expected",
        [
            (
                "-home-u-proj/combined_transcripts.html",
                "./-home-u-proj/combined_transcripts.html",
            ),
            (
                "javascript:alert(1)/combined_transcripts.html",
                "./javascript%3Aalert%281%29/combined_transcripts.html",
            ),
            ("p/session-x\" y='z'.html", "./p/session-x%22%20y%3D%27z%27.html"),
            ("./already/relative.html", "./already/relative.html"),
            ("../up/one.html", "../up/one.html"),
            ("proj/", "./proj/"),
            # Never absolute, never a `//host` network path.
            ("/abs/x.html", "./abs/x.html"),
            ("//evil.example/x.html", "./evil.example/x.html"),
        ],
    )
    def test_shapes(self, path: str, expected: str) -> None:
        from claude_code_log.utils import relative_href

        assert relative_href(path) == expected

    @pytest.mark.skipif(os.sep != "\\", reason="backslash is a separator only here")
    def test_windows_separators(self) -> None:
        from claude_code_log.utils import relative_href

        assert relative_href("a\\b\\c.html") == "./a/b/c.html"
        assert relative_href("\\\\evil.example\\x.html") == "./evil.example/x.html"

    @pytest.mark.skipif(os.sep == "\\", reason="backslash is a separator on Windows")
    def test_backslash_in_a_name_is_encoded(self) -> None:
        # A directory named `\\evil.example\x` would otherwise become the
        # network path `//evil.example/x`, which browsers resolve off-box.
        from claude_code_log.utils import relative_href

        assert (
            relative_href("\\\\evil.example\\x/combined_transcripts.html")
            == "./%5C%5Cevil.example%5Cx/combined_transcripts.html"
        )


class TestSearchLinks:
    """`serve`'s archive search links, built from cached ids."""

    def test_hostile_session_id_is_url_quoted(self) -> None:
        link = build_link("proj", HOSTILE_SESSION, "u1", "assistant", "q")
        assert '"' not in link and " " not in link and "=" not in link.split("?")[0]
        assert link.startswith("./proj/session-x%22%20onmouseover%3D")

    def test_single_quote_session_id_is_url_quoted(self) -> None:
        link = build_link("proj", HOSTILE_SESSION_SQ, None, "assistant", "")
        assert "'" not in link and " " not in link

    def test_scheme_like_slug_stays_relative(self) -> None:
        link = build_link(SCHEME_PROJECT, "s1", None, "assistant", "")
        assert link == "./javascript%3Aalert%281%29/session-s1.html"
        assert build_link(SCHEME_PROJECT, None, None, "assistant", "") == (
            "./javascript%3Aalert%281%29/"
        )


# ---- the index page, end to end ---------------------------------------


def _row(session_id: str, uuid: str, ts: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp/x",
        "sessionId": session_id,
        "version": "1.0",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


UNC_PROJECT = "\\\\evil.example\\x"


def write_hostile_archive(projects: Path) -> Path:
    """Three projects: one whose session id breaks out of a quoted attribute,
    one whose directory name is a ``javascript:`` URL, and one whose name
    would read as a ``//host`` network path. Shared with the browser test."""
    quoted = projects / "-tmp-quoted"
    quoted.mkdir(parents=True)
    rows = [
        _row(HOSTILE_SESSION, "u1", "2025-01-01T00:00:01Z", "hello findme"),
        _row(HOSTILE_SESSION_SQ, "u2", "2025-01-01T00:00:02Z", "again findme"),
    ]
    (quoted / "s.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    scheme = projects / SCHEME_PROJECT
    scheme.mkdir()
    (scheme / "s.jsonl").write_text(
        json.dumps(_row("s1", "u3", "2025-01-01T00:00:03Z", "scheme findme")) + "\n",
        encoding="utf-8",
    )
    unc = projects / UNC_PROJECT
    unc.mkdir()
    (unc / "s.jsonl").write_text(
        json.dumps(_row("s2", "u4", "2025-01-01T00:00:04Z", "unc findme")) + "\n",
        encoding="utf-8",
    )
    return projects


@posix_only
@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_index_links_are_relative_and_closed(tmp_path: Path, theme: str) -> None:
    projects = write_hostile_archive(tmp_path / "projects")
    process_projects_hierarchy(projects, silent=True, theme=theme)
    html = (projects / "index.html").read_text(encoding="utf-8")

    elements = _elements(html)
    handlers = [
        (tag, name)
        for tag, attrs in elements
        for name in attrs
        if name.startswith("on")
    ]
    assert handlers == []

    hrefs = [attrs["href"] for tag, attrs in elements if tag == "a" and "href" in attrs]
    # Nothing absolute or network-relative (`//evil.example/...`).
    assert not [h for h in hrefs if h.startswith("/") or h.startswith("\\")], hrefs
    project_hrefs = [h for h in hrefs if "javascript" in h or "-tmp-quoted" in h]
    # Both projects' combined links and their session links are there.
    assert len(project_hrefs) >= 5, hrefs
    for href in project_hrefs:
        assert href.startswith("./"), href
        assert '"' not in href and "'" not in href, href
    assert "./javascript%3Aalert%281%29/combined_transcripts.html" in hrefs


@posix_only
def test_recent_payload_links_are_relative(tmp_path: Path) -> None:
    projects = write_hostile_archive(tmp_path / "projects")
    process_projects_hierarchy(projects, silent=True)
    from datetime import datetime, timezone

    payload = build_recent_payload(
        projects, now=datetime(2025, 1, 1, 0, 10, tzinfo=timezone.utc)
    )
    hrefs = [s["href"] for s in payload["sessions"]]
    assert hrefs, payload
    for href in hrefs:
        assert href.startswith("./"), href
        assert '"' not in href and "'" not in href, href

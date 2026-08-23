"""Tests for full-archive search.

Weighted towards the text extractor and the two invariants that are easy to
break silently: the `CROSS JOIN` query plan and base64 exclusion. Both were
found by measurement rather than by reading the code, and neither shows up
as a failure — one is a 840x slowdown, the other a 28% larger index.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Optional

import pytest

from claude_code_log.search import (
    DEFAULT_INDEX_FIELDS,
    DEFAULT_SEARCH_FIELDS,
    FTS_TABLE,
    SEARCH_FIELDS,
    SKIP_TYPES,
    build_excerpt,
    build_link,
    count_matches,
    decode_entry,
    ensure_index,
    extract_search_text,
    fts5_available,
    fts_escape,
    index_status,
    parse_field_spec,
    reindex_files,
    search,
)

# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def test_extracts_assistant_text_and_thinking() -> None:
    entry: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "the visible answer"},
                {"type": "thinking", "thinking": "the private reasoning"},
            ]
        },
    }
    out = extract_search_text(entry)
    assert out["text"] == "the visible answer"
    assert out["thinking"] == "the private reasoning"
    assert out["tool_input"] == ""


def test_extracts_tool_use_name_and_input() -> None:
    entry: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Grep",
                    "input": {"pattern": "needle", "path": "/src"},
                }
            ]
        },
    }
    out = extract_search_text(entry)
    assert "Grep" in out["tool_input"]
    assert "needle" in out["tool_input"]
    assert out["text"] == ""


def test_tool_result_text_does_not_leak_into_the_text_group() -> None:
    """A file dump must not look like prose the user wrote.

    `tool_result` is excluded from the default search fields precisely
    because it is noisy, so nested text blocks landing in `text` would
    defeat that.
    """
    entry: dict[str, Any] = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "line one of a file dump"}],
                }
            ]
        },
    }
    out = extract_search_text(entry)
    assert "file dump" in out["tool_result"]
    assert out["text"] == ""


def test_base64_image_payloads_are_excluded() -> None:
    """The 28%-index-inflation bug: naive flattening indexes screenshots."""
    payload = "iVBORw0KGgoAAAANSUhEUg" + "A" * 4000
    entry: dict[str, Any] = {
        "type": "user",
        "message": {
            "content": [
                {"type": "text", "text": "here is a screenshot"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": payload,
                    },
                },
            ]
        },
    }
    out = extract_search_text(entry)
    assert out["text"] == "here is a screenshot"
    for value in out.values():
        assert payload not in value
        assert "iVBORw0KGgo" not in value


def test_queue_operation_content_items_are_walked_not_flattened() -> None:
    """A real archive had a 6.1 MB queue-operation that was one line + a PNG."""
    payload = "iVBORw0KGgo" + "B" * 3000
    entry: dict[str, Any] = {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": [
            {"type": "text", "text": "the queued prompt"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": payload,
                },
            },
        ],
    }
    out = extract_search_text(entry)
    assert out["text"] == "the queued prompt"
    assert all(payload not in v for v in out.values())


def test_short_base64_like_tokens_survive() -> None:
    """The filter is deliberately conservative — real identifiers must pass."""
    entry: dict[str, Any] = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "sha256:abc123DEF456=="}]},
    }
    assert "sha256:abc123DEF456==" in extract_search_text(entry)["text"]


@pytest.mark.parametrize(
    "entry,group,needle",
    [
        ({"type": "summary", "summary": "a session summary"}, "meta", "summary"),
        ({"type": "ai-title", "aiTitle": "Generated Title"}, "meta", "Generated"),
        ({"type": "system", "content": "a system note"}, "meta", "system note"),
        (
            {"type": "attachment", "attachment": {"type": "file", "text": "attached"}},
            "attachment",
            "attached",
        ),
    ],
)
def test_extracts_non_message_entry_types(
    entry: dict[str, Any], group: str, needle: str
) -> None:
    assert needle in extract_search_text(entry)[group]


def test_extraction_always_returns_every_group() -> None:
    out = extract_search_text({"type": "user", "message": {"content": []}})
    assert set(out) == set(SEARCH_FIELDS)
    assert all(isinstance(v, str) for v in out.values())


def test_booleans_are_not_indexed_as_words() -> None:
    """`true`/`false` appear in nearly every tool input; they're pure noise."""
    entry: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Edit", "input": {"replace_all": True}}
            ]
        },
    }
    assert "True" not in extract_search_text(entry)["tool_input"]


def test_deeply_nested_structures_terminate() -> None:
    nested: Any = "bottom"
    for _ in range(50):
        nested = {"next": nested}
    entry: dict[str, Any] = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "X", "input": nested}]},
    }
    extract_search_text(entry)  # must not recurse forever


def test_decode_entry_roundtrip() -> None:
    entry = {"type": "user", "message": {"content": "hi"}}
    blob = zlib.compress(json.dumps(entry).encode("utf-8"))
    assert decode_entry(blob) == entry


# ---------------------------------------------------------------------------
# Query escaping
# ---------------------------------------------------------------------------


def test_hyphenated_terms_are_escaped() -> None:
    """`vis-timeline` is a hard OperationalError unquoted."""
    assert fts_escape("vis-timeline") == '"vis-timeline"'


@pytest.mark.parametrize(
    "raw",
    [
        "vis-timeline",
        "NOT",
        "AND OR",
        "a:b",
        "foo(bar)",
        "^caret",
        'say "hello',
        "-leading-dash",
        "*",
    ],
)
def test_escaped_queries_are_valid_fts_syntax(raw: str) -> None:
    """Any user input must produce a runnable MATCH, never an exception."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    db.execute("INSERT INTO t(x) VALUES('some text')")
    escaped = fts_escape(raw)
    if escaped:
        db.execute("SELECT rowid FROM t WHERE t MATCH ?", (escaped,)).fetchall()


def test_quoted_phrase_is_preserved() -> None:
    assert fts_escape('"exact phrase"') == '"exact phrase"'


def test_trailing_star_keeps_prefix_semantics() -> None:
    assert fts_escape("tokeniz*") == '"tokeniz"*'


def test_embedded_quotes_are_doubled() -> None:
    escaped = fts_escape('say"it')
    assert escaped == '"say""it"'


def test_empty_query_escapes_to_empty() -> None:
    assert fts_escape("   ") == ""


# ---------------------------------------------------------------------------
# Field specs
# ---------------------------------------------------------------------------


def test_field_spec_defaults_exclude_tool_result() -> None:
    assert "tool_result" not in DEFAULT_SEARCH_FIELDS
    assert "tool_result" in DEFAULT_INDEX_FIELDS


@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, DEFAULT_SEARCH_FIELDS),
        ("", DEFAULT_SEARCH_FIELDS),
        ("all", SEARCH_FIELDS),
        ("none", ()),
        ("text", ("text",)),
        ("thinking,text", ("text", "thinking")),  # normalised to canonical order
        ("+tool_result", SEARCH_FIELDS),
        ("-thinking", ("text", "tool_input", "attachment", "meta")),
        (
            "+tool_result,-text",
            ("thinking", "tool_input", "tool_result", "attachment", "meta"),
        ),
    ],
)
def test_parse_field_spec(spec: Optional[str], expected: tuple[str, ...]) -> None:
    assert parse_field_spec(spec, DEFAULT_SEARCH_FIELDS) == expected


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown search field"):
        parse_field_spec("nonsense", DEFAULT_SEARCH_FIELDS)


def test_mixing_absolute_and_delta_is_rejected() -> None:
    with pytest.raises(ValueError, match="mixes absolute"):
        parse_field_spec("text,+tool_result", DEFAULT_SEARCH_FIELDS)


# ---------------------------------------------------------------------------
# Links and excerpts
# ---------------------------------------------------------------------------


def test_link_carries_both_uuid_and_query() -> None:
    """uuid anchors the card; q lets in-page search open collapsed details."""
    link = build_link("proj-slug", "sess-1", "uuid-9", "assistant", "needle")
    assert link.startswith("proj-slug/session-sess-1.html?")
    assert "uuid=uuid-9" in link
    assert "q=needle" in link


@pytest.mark.parametrize("message_type", ["ai-title", "queue-operation"])
def test_types_without_a_rendered_card_get_session_links(message_type: str) -> None:
    link = build_link("proj", "sess-1", "uuid-9", message_type, "needle")
    assert "uuid=" not in link
    assert "session-sess-1.html" in link


def test_link_without_a_session_falls_back_to_the_project() -> None:
    assert build_link("proj", None, "uuid", "assistant", "q") == "proj/"


def test_excerpt_centres_on_the_match() -> None:
    text = "padding " * 50 + "NEEDLE" + " trailing" * 50
    excerpt = build_excerpt(text, ["needle"], width=60)
    assert "NEEDLE" in excerpt
    assert excerpt.startswith("…")
    assert len(excerpt) < 120


def test_excerpt_without_a_match_returns_a_prefix() -> None:
    assert build_excerpt("some text", ["absent"]).startswith("some text")


def test_excerpt_of_empty_text_is_empty() -> None:
    assert build_excerpt("", ["x"]) == ""


# ---------------------------------------------------------------------------
# Index lifecycle and querying, against a real cache schema
# ---------------------------------------------------------------------------


def _make_cache(tmp_path: Path) -> sqlite3.Connection:
    """A cache DB with the real schema, populated by hand."""
    from claude_code_log.migrations.runner import run_migrations

    db_path = tmp_path / "cache.db"
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO projects(id, project_path, version, cache_created, last_updated) "
        "VALUES(1, '/home/u/.claude/projects/-home-u-alpha', '1', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO projects(id, project_path, version, cache_created, last_updated) "
        "VALUES(2, '/home/u/.claude/projects/-home-u-beta', '1', 'x', 'x')"
    )
    for file_id, project_id in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO cached_files(id, project_id, file_name, file_path, "
            "source_mtime, cached_mtime, message_count) VALUES(?, ?, ?, ?, 0, 0, 0)",
            (file_id, project_id, f"f{file_id}.jsonl", f"/tmp/f{file_id}.jsonl"),
        )
    conn.commit()
    return conn


def _add_message(
    conn: sqlite3.Connection,
    message_id: int,
    project_id: int,
    file_id: int,
    entry: dict[str, Any],
    *,
    session_id: str = "sess-1",
    uuid: str = "uuid-1",
    timestamp: str = "2026-01-01T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO messages(id, project_id, file_id, type, timestamp, session_id, "
        "_uuid, content) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            project_id,
            file_id,
            entry["type"],
            timestamp,
            session_id,
            uuid,
            zlib.compress(json.dumps(entry).encode("utf-8")),
        ),
    )
    conn.commit()


def _assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


@pytest.fixture
def cache(tmp_path: Path) -> sqlite3.Connection:
    conn = _make_cache(tmp_path)
    _add_message(conn, 1, 1, 1, _assistant("alpha project mentions pydantic"))
    _add_message(
        conn, 2, 1, 1, _assistant("alpha again, this one about sqlite"), uuid="uuid-2"
    )
    _add_message(
        conn,
        3,
        2,
        2,
        _assistant("beta project also mentions pydantic"),
        session_id="sess-2",
        uuid="uuid-3",
    )
    return conn


def test_fts5_is_available() -> None:
    assert fts5_available(sqlite3.connect(":memory:"))


def test_ensure_index_builds_and_reports_status(cache: sqlite3.Connection) -> None:
    before = index_status(cache)
    assert before.ready is False

    status = ensure_index(cache)
    assert status.ready is True
    assert status.indexed_messages == 3
    assert status.fields == DEFAULT_INDEX_FIELDS
    assert status.built_at


def test_search_finds_across_projects(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    results = search(cache, "pydantic")
    assert len(results) == 2
    assert {r.project_slug for r in results} == {"-home-u-alpha", "-home-u-beta"}


def test_search_filters_by_project(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    results = search(cache, "pydantic", project_id=2)
    assert len(results) == 1
    assert results[0].project_slug == "-home-u-beta"


def test_search_filters_by_session(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    results = search(cache, "pydantic", session_id="sess-2")
    assert [r.session_id for r in results] == ["sess-2"]


def test_search_results_are_linkable(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    result = search(cache, "sqlite")[0]
    assert result.link == "-home-u-alpha/session-sess-1.html?uuid=uuid-2&q=sqlite"
    assert result.field == "text"
    assert "sqlite" in result.snippet


def test_search_respects_field_selection(cache: sqlite3.Connection) -> None:
    _add_message(
        cache,
        4,
        1,
        1,
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "a rare_token in tool output"}
                ]
            },
        },
        uuid="uuid-4",
    )
    ensure_index(cache, rebuild=True)

    assert search(cache, "rare_token", fields=DEFAULT_SEARCH_FIELDS) == []
    hits = search(cache, "rare_token", fields=SEARCH_FIELDS)
    assert len(hits) == 1
    assert hits[0].field == "tool_result"


def test_skip_types_are_not_indexed(cache: sqlite3.Connection) -> None:
    _add_message(
        cache,
        5,
        1,
        1,
        {"type": "progress", "message": {"content": [{"type": "text", "text": "x"}]}},
        uuid="uuid-5",
    )
    ensure_index(cache, rebuild=True)
    assert "progress" in SKIP_TYPES
    indexed = cache.execute(f"SELECT count(*) FROM {FTS_TABLE}").fetchone()[0]
    assert indexed == 3


def test_unquotable_query_does_not_raise(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    assert search(cache, "vis-timeline") == []
    assert search(cache, "") == []


def test_count_matches(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    assert count_matches(cache, "pydantic") == 2
    assert count_matches(cache, "nonexistentterm") == 0


# --- the invariants -------------------------------------------------------


def test_filtered_search_keeps_fts_as_the_outer_loop(
    cache: sqlite3.Connection,
) -> None:
    """The CROSS JOIN is load-bearing: a plain JOIN is 840x slower.

    With `JOIN`, SQLite drives from `messages` (SEARCH m USING INDEX) and
    probes FTS by rowid, which on a real archive turned a 2.4 ms search into
    2,012 ms. Nothing fails when that regresses — it just gets slow — so
    the plan itself is asserted.
    """
    ensure_index(cache)
    from claude_code_log.search import _build_query  # pyright: ignore[reportPrivateUsage]

    sql, _ = _build_query(
        project_id=1,
        session_id=None,
        message_type=None,
        date_from=None,
        date_to=None,
    )
    assert "CROSS JOIN" in sql

    plan = [
        str(row[-1])
        for row in cache.execute(f"EXPLAIN QUERY PLAN {sql}", ('"pydantic"', 1, 20, 0))
    ]
    plan_text = " | ".join(plan)

    # The healthy plan scans the FTS virtual table and probes `messages` by
    # rowid. The pathological one is the reverse:
    #   SEARCH m USING COVERING INDEX ... (project_id=?)
    #   SCAN f VIRTUAL TABLE INDEX 32:=M6      <- note the `=`: rowid-constrained
    assert "SCAN f VIRTUAL TABLE" in plan_text, plan_text
    assert "SEARCH m USING INTEGER PRIMARY KEY" in plan_text, plan_text
    assert "SCAN m" not in plan_text, plan_text


def test_index_rebuilds_when_the_extractor_version_changes(
    cache: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed extractor must not leave stale rows behind."""
    ensure_index(cache)
    assert search(cache, "pydantic")

    monkeypatch.setattr("claude_code_log.search.EXTRACTOR_VERSION", 999)
    status = ensure_index(cache)
    assert status.indexed_messages == 3
    assert search(cache, "pydantic")


def test_index_rebuilds_when_fields_change(cache: sqlite3.Connection) -> None:
    ensure_index(cache, index_fields=("text",))
    assert index_status(cache).fields == ("text",)
    ensure_index(cache, index_fields=SEARCH_FIELDS)
    assert index_status(cache).fields == SEARCH_FIELDS


def test_reindex_files_picks_up_changed_content(cache: sqlite3.Connection) -> None:
    """The incremental hook: ~180 ms for the largest file in an 8 GB archive."""
    ensure_index(cache)
    assert count_matches(cache, "pydantic") == 2

    cache.execute("DELETE FROM messages WHERE id = 1")
    _add_message(cache, 10, 1, 1, _assistant("replaced with mercurial"), uuid="uuid-10")
    reindex_files(cache, [1])

    assert count_matches(cache, "pydantic") == 1
    assert count_matches(cache, "mercurial") == 1


def test_incremental_index_adds_a_new_file(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    cache.execute(
        "INSERT INTO cached_files(id, project_id, file_name, file_path, "
        "source_mtime, cached_mtime, message_count) "
        "VALUES(3, 1, 'f3.jsonl', '/tmp/f3.jsonl', 0, 0, 0)"
    )
    _add_message(cache, 20, 1, 3, _assistant("a brand new file"), uuid="uuid-20")

    status = ensure_index(cache)
    assert status.indexed_messages == 4
    assert count_matches(cache, "brand") == 1


def test_removing_a_file_removes_its_rows(cache: sqlite3.Connection) -> None:
    ensure_index(cache)
    assert count_matches(cache, "sqlite") == 1
    cache.execute("DELETE FROM messages WHERE file_id = 1")
    cache.execute("DELETE FROM cached_files WHERE id = 1")
    cache.commit()

    ensure_index(cache)
    assert count_matches(cache, "sqlite") == 0
    assert count_matches(cache, "pydantic") == 1


def test_searching_before_the_index_exists_raises(cache: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="not been built"):
        search(cache, "pydantic")

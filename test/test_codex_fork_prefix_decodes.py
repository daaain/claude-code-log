"""Discovery already knows each fork's inherited prefix; loading must not
recompute it, and a shared parent must not be decoded once per child.

Two redundancies, measured on a 34-rollout archive as 132 of 236 decodes:

* the load path recomputed ``inherited_prefix_records`` that discovery had
  already computed and published, re-decoding both the child and its parent to
  do it; and
* ``_with_inherited_prefix`` decoded a parent once per child, so a parent shared
  by 12 forks was fully decoded 12 times in one discovery pass.

As with the totals seam, **correct output is compatible with arbitrarily
redundant decoding** — which is why this survived a green suite for as long as
it did — so these tests count the primitive rather than checking the rendering.

They also pin the two ways the reduction can be wrong rather than slow, both of
which are silent:

* a duplicated thread id is *retained* by discovery but *illegal* to load, so
  the index holds an identity for an id whose load must raise. The ambiguity
  check has to stay ahead of the fast path.
* ``inherited_prefix_records == 0`` cannot be distinguished from "not yet
  computed" if membership is inferred from the value, and 0 is the common case.
  Fork children whose prefix is genuinely 0 would then be recomputed forever —
  a fix that silently underdelivers with nothing red.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from claude_code_log.converter import render_provider_wholesale
from claude_code_log.providers import codex as codex_module
from claude_code_log.providers.codex import CodexProvider, _DecodedRecord

_CWD = "/proj/fork"
_DAY = "2026-03-04"


def _meta(thread_id: str, parent: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": thread_id,
        "timestamp": f"{_DAY}T00:00:00Z",
        "cwd": _CWD,
    }
    if parent is not None:
        payload["parent_thread_id"] = parent
    return {
        "timestamp": f"{_DAY}T00:00:00Z",
        "type": "session_meta",
        "payload": payload,
    }


def _msg(text: str) -> dict[str, Any]:
    """A record identified by its payload alone.

    ``_same_semantic_record`` compares kind and payload and ignores the envelope
    timestamp, so a shared record must carry an identical payload — the same
    text — while the timestamp is free to differ.
    """
    return {
        "timestamp": f"{_DAY}T00:00:01Z",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    }


def _write(root: Path, name: str, records: list[dict[str, Any]]) -> Path:
    path = root / f"rollout-{_DAY}T00-00-00-{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _tid(n: int) -> str:
    return f"4000000{n}-0000-4000-8000-00000000000{n}"


def _fork_tree(root: Path, child_count: int) -> tuple[Path, list[Path]]:
    """A parent with ``child_count`` forks inheriting its tail.

    The parent's candidate records are ``[a, b, c]``.
    ``_contiguous_prefix_length`` only accepts a run reaching the **end** of the
    parent and at least 2 long, so a child inherits by *starting* with the
    parent's tail. Children deliberately inherit **different** amounts (2 then
    3, alternating), because a grouping bug that hands every child the same
    parent-derived number would pass a fixture where they all agree.
    """
    root.mkdir(parents=True, exist_ok=True)
    parent_id = _tid(1)
    parent = _write(root, "parent", [_meta(parent_id), _msg("a"), _msg("b"), _msg("c")])
    children: list[Path] = []
    for i in range(child_count):
        inherited = (
            [_msg("b"), _msg("c")] if i % 2 == 0 else [_msg("a"), _msg("b"), _msg("c")]
        )
        child_id = _tid(i + 2)
        children.append(
            _write(
                root,
                f"child{i}",
                [_meta(child_id, parent=parent_id), *inherited, _msg(f"own-{i}")],
            )
        )
    return parent, children


def _decodes_per_path(root: Path, out: Path) -> Counter:
    """Per-path ``_decode_records`` counts across one wholesale render."""
    counts: Counter = Counter()
    original = CodexProvider._decode_records

    def wrapped(self, path: Path) -> Iterator[_DecodedRecord]:
        counts[Path(path).name] += 1
        return iter(list(original(self, path)))

    codex_module.CodexProvider._decode_records = wrapped  # ty: ignore[invalid-assignment]
    try:
        render_provider_wholesale("codex", root, out, use_cache=False, silent=True)
    finally:
        codex_module.CodexProvider._decode_records = original
    return counts


def test_load_does_not_recompute_the_inherited_prefix(tmp_path: Path) -> None:
    """Every rollout is decoded a bounded number of times, none of them a
    recomputation of what discovery published.

    The per-path budget after the fix, asserted as an exact map because it is
    deterministic:

    * 1 header read in the index build (early exit, not a full materialisation)
    * 1 full decode during discovery for a fork child, measured against its
      parent's records
    * 1 full decode during discovery for a rollout that *is* a parent
    * 1 full decode for the session's own load

    So a plain fork child is 3. Before this change the same child cost 6: the
    load path re-resolved the identity — decoding the child *and* its parent
    again — and then decoded the child a third time for its records.

    Asserted over the *children* only. The shared parent's count is not a
    property of this change: it still scales with the fan-out until discovery
    groups by parent, which
    :func:`test_shared_parent_decode_count_is_independent_of_child_count` owns.
    Asserting it here would make this test fail for a reason it does not fix.
    """
    root = tmp_path / "sessions"
    _parent, children = _fork_tree(root, 2)

    counts = _decodes_per_path(root, tmp_path / "out")

    for child in children:
        assert counts[child.name] == 3, f"child {child.name}: {dict(counts)}"


def test_duplicate_thread_id_still_raises_after_discovery(tmp_path: Path) -> None:
    """The ambiguity check stays ahead of the resolved-identity lookup.

    Discovery *retains* the lexicographically first rollout for a duplicated
    thread id and warns; loading that id must *raise*. So the index legitimately
    holds a usable identity for an id that is illegal to load, and consulting it
    before the ambiguity test would quietly load the first rollout instead.

    Discovery runs first here deliberately — that is what populates the map and
    makes the fast path available to be wrongly taken. Without the preceding
    discovery this test passes whatever the ordering, which is exactly the
    weakening to guard against.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    duped = _tid(1)
    _write(root, "first", [_meta(duped), _msg("a"), _msg("b")])
    _write(root, "second", [_meta(duped), _msg("c"), _msg("d")])

    provider = CodexProvider()
    discovered = list(provider.discover_sessions_under(root))
    assert len(discovered) == 1, "discovery should retain exactly one of the two"

    with pytest.raises(ValueError, match="Multiple Codex rollouts have thread id"):
        list(provider.load_session_under(root, duped))


def test_zero_prefix_fork_is_not_recomputed(tmp_path: Path) -> None:
    """A computed prefix of **0** must count as computed.

    This is the fixture the obvious implementation gets wrong. If membership in
    the resolved map is inferred from ``inherited_prefix_records > 0`` instead of
    from presence, every session whose prefix is 0 falls back to the slow path —
    and 0 is the common case.

    It has to be a fork child with a *uniquely resolvable parent* and no shared
    tail. A plain non-fork session cannot detect the defect: with no parent,
    ``_with_inherited_prefix`` returns before decoding anything, so recomputing
    it costs zero decodes and is invisible. Here the recomputation decodes the
    child and the parent again, taking both from 3 to 4.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    parent_id, child_id = _tid(1), _tid(2)
    parent = _write(root, "parent", [_meta(parent_id), _msg("a"), _msg("b")])
    # Shares nothing with the parent's tail, so the prefix is a genuine 0.
    child = _write(
        root, "child", [_meta(child_id, parent=parent_id), _msg("x"), _msg("y")]
    )

    provider = CodexProvider()
    prefixes = {
        info.session_id: getattr(info, "inherited_prefix_records", None)
        for info in provider.discover_sessions_under(root)
    }
    assert prefixes[child_id] == 0, (
        "fixture must produce a genuine zero prefix, not a missing parent"
    )

    counts = _decodes_per_path(root, tmp_path / "out")
    assert counts[child.name] == 3, (
        f"zero-prefix child was recomputed at load: {dict(counts)}"
    )
    assert counts[parent.name] == 3, (
        f"parent re-decoded for a zero-prefix child's recomputation: {dict(counts)}"
    )

"""Unit tests for the fragment store's content digest.

The digest replaces the retained ``MessageContent`` reference in the
store's hit-verification (work/render-format-once.md § 4.9 — retaining
one content per fragment pinned +270MB of object graphs on a large
archive). Its contract is: **same field coverage as dataclass equality**,
so a stored fragment is served iff the requesting tree's content would
have compared equal to the one that produced it. These tests pin the
directions that matter:

- equality-relevant state (including nested pydantic models and their
  ``extra="allow"`` fields) must change the digest;
- the per-tree ``compare=False`` fields must NOT change it (they differ
  between the combined-page and per-session trees for the *same*
  message — a digest sensitive to them would kill every hit);
- distinct content classes must never collide, even with identical
  field values (dataclass ``__eq__`` requires identical classes).

The end-to-end byte-equivalence proof lives in
``test_render_cache_equivalence.py::test_fragment_store_render_is_byte_identical``.
"""

from claude_code_log.fragment_store import RenderFragmentStore, content_digest
from claude_code_log.models import (
    GrepInput,
    MessageMeta,
    SystemMessage,
    ToolUseMessage,
)


def _system(text: str = "hello", level: str = "info") -> SystemMessage:
    return SystemMessage(meta=MessageMeta.empty("u1"), level=level, text=text)


def test_equal_contents_have_equal_digests():
    assert content_digest(_system()) == content_digest(_system())


def test_per_tree_fields_do_not_affect_digest():
    # The same message gets different message_index / fragment_key values
    # in the combined tree vs its session tree; both are compare=False
    # and must be invisible to the digest, exactly as they are to __eq__.
    a, b = _system(), _system()
    a.message_index = 7
    a.fragment_key = (123, 0)
    b.message_index = 991
    b.fragment_key = (456, 2)
    assert a == b
    assert content_digest(a) == content_digest(b)


def test_compare_field_change_changes_digest():
    assert content_digest(_system("one")) != content_digest(_system("two"))
    assert content_digest(_system(level="info")) != content_digest(
        _system(level="error")
    )


def test_nested_meta_change_changes_digest():
    a = _system()
    b = _system()
    b.meta.cwd = "/somewhere/else"
    assert a != b
    assert content_digest(a) != content_digest(b)


def test_distinct_classes_never_collide():
    # Dataclass __eq__ requires identical classes; the digest tags the
    # class, so structurally similar contents of different types differ.
    meta = MessageMeta.empty("u1")
    tool = ToolUseMessage(
        meta=meta,
        input=GrepInput(pattern="hello"),
        tool_use_id="t1",
        tool_name="Grep",
    )
    assert content_digest(tool) != content_digest(_system())


def test_pydantic_extra_fields_affect_digest():
    # extra="allow" models carry unknown transcript fields in
    # __pydantic_extra__, and pydantic equality includes them.
    plain = GrepInput.model_validate({"pattern": "x"})
    with_extra = GrepInput.model_validate({"pattern": "x", "-i": True})
    with_same_extra = GrepInput.model_validate({"pattern": "x", "-i": True})

    def tool_with(inp: GrepInput) -> ToolUseMessage:
        return ToolUseMessage(
            meta=MessageMeta.empty("u1"),
            input=inp,
            tool_use_id="t1",
            tool_name="Grep",
        )

    assert content_digest(tool_with(plain)) != content_digest(tool_with(with_extra))
    assert content_digest(tool_with(with_extra)) == content_digest(
        tool_with(with_same_extra)
    )


def test_stable_key_translates_ids_to_master_list_ordinals():
    store = RenderFragmentStore()
    entries = [object(), object(), object()]
    store.set_entry_ordinals(entries)

    # A stamped (id(entry), part) maps to the entry's position in the
    # master list — the identity two trees (or two processes loading the
    # same list) agree on.
    assert store.stable_key((id(entries[2]), 0)) == (2, 0)
    assert store.stable_key((id(entries[0]), 5)) == (0, 5)

    # An id the master list doesn't contain declines caching.
    stranger = object()
    assert store.stable_key((id(stranger), 0)) is None


def test_store_hit_requires_matching_digest():
    store = RenderFragmentStore()
    key = (1, 0, "sig")
    fragment = ("title", "<p>html</p>", "12:00")
    store.put(key, _system(), fragment)

    # Equal content (fresh object, different per-tree fields) → hit.
    requester = _system()
    requester.message_index = 42
    assert store.get(key, requester) == fragment
    assert store.hits == 1

    # Diverged content under the same key → conflict, served fresh.
    assert store.get(key, _system("changed")) is None
    assert store.conflicts == 1

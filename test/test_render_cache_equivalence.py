"""End-to-end proof that the render optimisations don't change output.

Both optimisations in this area are pure performance work, and both are
easy to get subtly wrong in ways unit tests won't catch:

- **Memoization** trades a recompute for a cached string. It is only sound
  if the memoized function is genuinely pure with respect to its key —
  Markdown is not purely a function of its text (the SHA-linkifier reads
  the active render repo cwd), so a missing key component would show up
  here as a rendered difference rather than an exception.
- **The render fan-out** re-derives each worker's state from the cache
  instead of inheriting the parent's, and moves the paginated "reveal the
  Next link" fixup from a per-page step to a single post-pass. Either
  could plausibly diverge from the serial path.

So this converts a real multi-session project three ways and compares the
bytes. It runs the conversion for real rather than stubbing the renderer:
the whole point is to exercise the integration, and a project directory
from ``test_data/real_projects`` covers Pygments-heavy tool output,
Markdown, sub-agents and multiple sessions.
"""

import shutil
from pathlib import Path

import pytest

from claude_code_log import converter, render_cache
from claude_code_log import render_pool as render_pool_module
from claude_code_log.converter import convert_jsonl_to
from claude_code_log.render_pool import RenderPool, RenderUnit

# Real project with 40 session files — enough for several combined pages
# and a wide spread of message types.
SOURCE_PROJECT = (
    Path(__file__).parent
    / "test_data"
    / "real_projects"
    / "-Users-dain-workspace-coderabbit-review-helper"
)

# Small enough to force several combined pages out of this project, so the
# pagination path (and its Next-link fixup) is exercised, not just the
# single-file path.
PAGE_SIZE = 200


def _convert_copy(tmp_path: Path, name: str, **kwargs: object) -> dict[str, bytes]:
    """Convert a fresh copy of the project and return {filename: bytes}.

    Each run gets its own copy so it also gets its own cache DB (which
    lives beside the project directory) — otherwise the second run would
    find the first's output current and skip it, comparing nothing.

    The copy keeps the project's *own* directory name and varies only the
    parent, because the rendered title is derived from the project
    directory name: copying to ``<tmp>/memoized`` would make every page
    differ on the title alone and the comparison would be meaningless.
    """
    work_dir = tmp_path / name / SOURCE_PROJECT.name
    shutil.copytree(SOURCE_PROJECT, work_dir)

    convert_jsonl_to("html", work_dir, page_size=PAGE_SIZE, silent=True, **kwargs)  # type: ignore[arg-type]

    rendered = {
        path.name: path.read_bytes() for path in sorted(work_dir.glob("*.html"))
    }
    assert rendered, "conversion produced no HTML"
    return rendered


def _assert_same(
    baseline: dict[str, bytes], other: dict[str, bytes], what: str
) -> None:
    assert set(other) == set(baseline), f"{what} produced a different set of files"
    differing = [name for name in baseline if baseline[name] != other[name]]
    assert not differing, f"{what} changed the bytes of: {', '.join(sorted(differing))}"


@pytest.fixture(autouse=True)
def _clean_caches():
    render_cache.clear_all()
    yield
    render_cache.clear_all()


def test_memoized_render_is_byte_identical_to_unmemoized(tmp_path: Path):
    """The memo must be invisible in the output — only in the clock."""
    with render_cache.disabled():
        baseline = _convert_copy(tmp_path, "unmemoized")
    memoized = _convert_copy(tmp_path, "memoized")

    _assert_same(baseline, memoized, "memoization")
    # Guard against the test silently passing because the memo never
    # engaged (e.g. a future refactor routes around it).
    stats = render_cache.pygments_cache.stats()
    assert stats["hits"] > 0, "memo never hit — the comparison proved nothing"


def test_fragment_store_render_is_byte_identical(tmp_path: Path, monkeypatch):
    """The fragment store must be invisible in the output — only in the clock.

    Both runs disable the leaf memo so the comparison isolates the store:
    a fragment served from the store replaces a *complete* title/html/
    timestamp computation, so any key too coarse for real data (uuid reuse
    across forked sessions, part-ordinal drift between the combined and
    per-session passes) shows up here as changed bytes.
    """
    from claude_code_log.fragment_store import RenderFragmentStore

    monkeypatch.setenv("CLAUDE_CODE_LOG_FRAGMENT_STORE", "0")
    with render_cache.disabled():
        baseline = _convert_copy(tmp_path, "no-fragments")
    monkeypatch.delenv("CLAUDE_CODE_LOG_FRAGMENT_STORE")

    # Capture the store the conversion creates, to prove it engaged —
    # otherwise a future refactor that silently stops attaching it would
    # make this comparison vacuous.
    stores: list[RenderFragmentStore] = []
    original_make = converter._make_fragment_store

    def capturing_make(format_: str, **kwargs: int):
        store = original_make(format_, **kwargs)
        if store is not None:
            stores.append(store)
        return store

    monkeypatch.setattr(converter, "_make_fragment_store", capturing_make)

    with render_cache.disabled():
        fragmented = _convert_copy(tmp_path, "fragments")

    _assert_same(baseline, fragmented, "the fragment store")
    assert stores, "no fragment store was created"
    hits = sum(store.stats()["hits"] for store in stores)
    assert hits > 0, "fragment store never hit — the comparison proved nothing"


def test_parallel_render_is_byte_identical_to_serial(tmp_path: Path, monkeypatch):
    """Fanning the render out over worker processes must not change output.

    The thresholds that normally keep small projects off the pool are
    lowered here: this fixture is far below them, and the point is to
    exercise the worker path, not the heuristic that avoids it.
    """
    # The fragment store must be on regardless of the parent environment —
    # the fed-fragment assertions below are vacuous with it disabled.
    monkeypatch.delenv("CLAUDE_CODE_LOG_FRAGMENT_STORE", raising=False)
    monkeypatch.setattr(converter, "_MIN_MESSAGES_FOR_RENDER_POOL", 0)
    monkeypatch.setattr(converter, "_MIN_UNITS_FOR_RENDER_POOL", 2)
    # Pin available memory too. The pool declines when memory is tight, so
    # on a small CI runner this test would otherwise compare the inline
    # path against itself — or fail its dispatch assertion — depending on
    # what else happened to be resident.
    monkeypatch.setattr(
        render_pool_module, "available_memory_bytes", lambda: 64 * 1024**3
    )

    # Count what the pool actually accepted. Every path in the fan-out
    # falls back to inline rendering on trouble, so without this a broken
    # pool would compare the serial path against itself and pass.
    dispatched: list[RenderUnit] = []
    original_submit = RenderPool.submit

    def counting_submit(self: RenderPool, unit: RenderUnit):
        future = original_submit(self, unit)
        if future is not None:
            dispatched.append(unit)
        return future

    monkeypatch.setattr(RenderPool, "submit", counting_submit)

    # Capture the conversion's fragment store to prove the worker deltas
    # actually flowed back to the parent (page workers export their
    # fragments; the dispatch loop absorbs them). Without this, a broken
    # return path would silently degrade the session feed to nothing.
    stores = []
    original_make = converter._make_fragment_store

    def capturing_make(format_: str, **kwargs: int):
        store = original_make(format_, **kwargs)
        if store is not None:
            stores.append(store)
        return store

    monkeypatch.setattr(converter, "_make_fragment_store", capturing_make)

    serial = _convert_copy(tmp_path, "serial", render_jobs=1)
    parallel = _convert_copy(tmp_path, "parallel", render_jobs=2)

    _assert_same(serial, parallel, "the render fan-out")
    kinds = [unit.kind for unit in dispatched]
    assert "page" in kinds, "no combined page went through a worker"
    assert "session" in kinds, "no session file went through a worker"
    # Workers never load the transcript — every dispatched unit must carry
    # its own entry slice (with aligned master-list ordinals), or the pool
    # would be quietly rendering from nothing / falling back inline.
    for unit in dispatched:
        assert unit.entries, f"{unit.kind} {unit.key} dispatched without entries"
        assert unit.entry_ordinals is not None and len(unit.entry_ordinals) == len(
            unit.entries
        ), f"{unit.kind} {unit.key} ordinals not aligned with its entries"
    # The fed-fragment path (work/render-format-once.md § 2): page workers
    # return fragment deltas, the parent absorbs them, and dispatched
    # session units carry their session's slice.
    assert any(unit.kind == "session" and unit.fed_fragments for unit in dispatched), (
        "no session unit was fed fragments — the feed never engaged"
    )
    parallel_store = stores[-1]
    assert parallel_store.stats()["entries"] > 0, (
        "the parent store absorbed no worker fragment deltas"
    )

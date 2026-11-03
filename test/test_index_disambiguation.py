"""Index-label disambiguation + provider-aware title (generic, not codex-only).

``prepare_projects_index`` renders a project's display name as the basename of
its least-nested working directory. Two projects whose cwds share a basename
(two ``codex`` worktrees, several ``main`` worktrees) otherwise collide into one
label. These pin the collision-disambiguation pass and the provider title,
including the invariant that both are no-ops absent a collision / provider label
so existing (Claude) output stays byte-identical.
"""

from claude_code_log.renderer import prepare_projects_index, title_for_projects_index


def _summary(name: str, working_dirs: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "working_directories": working_dirs,
        "last_modified": 0.0,
        "html_file": f"{name}/combined_transcripts.html",
        "jsonl_count": 1,
        "message_count": 1,
    }


def _labels(summaries: list[dict[str, object]]) -> dict[str, str]:
    projects, _ = prepare_projects_index(summaries)
    return {p.name: p.display_name for p in projects}


# --------------------------------------------------------------------------
# B.1 collision disambiguation
# --------------------------------------------------------------------------
def test_no_collision_leaves_basenames_bare() -> None:
    """The common case: distinct basenames stay bare — a strict no-op, which is
    why existing collision-free indexes render byte-identically."""
    labels = _labels(
        [
            _summary("-a", ["/home/joe/alpha"]),
            _summary("-b", ["/home/joe/beta"]),
        ]
    )
    assert labels == {"-a": "alpha", "-b": "beta"}


def test_two_way_collision_disambiguates_by_one_parent() -> None:
    """Two projects both named ``app`` → one parent component each, not the
    whole path."""
    labels = _labels(
        [
            _summary("-a", ["/home/joe/frontend/app"]),
            _summary("-b", ["/home/joe/backend/app"]),
        ]
    )
    assert labels == {"-a": "frontend/app", "-b": "backend/app"}


def test_three_way_collision_extends_until_unique() -> None:
    """A 3-way collision where two also share the immediate parent must keep
    extending past depth 2 until every member is distinct."""
    labels = _labels(
        [
            _summary("-a", ["/p/x/codex"]),
            _summary("-b", ["/q/x/codex"]),
            _summary("-c", ["/r/codex"]),
        ]
    )
    # depth1 "codex"×3 collide; depth2 "x/codex","x/codex","r/codex" still
    # collide; depth3 separates.
    assert labels == {"-a": "p/x/codex", "-b": "q/x/codex", "-c": "r/codex"}


def test_partial_collision_leaves_the_unique_one_bare() -> None:
    """Two collide, a third has a unique basename → only the colliding pair is
    disambiguated; the unique label stays bare."""
    labels = _labels(
        [
            _summary("-a", ["/home/joe/x/codex"]),
            _summary("-b", ["/home/joe/y/codex"]),
            _summary("-c", ["/home/joe/foo"]),
        ]
    )
    assert labels == {"-a": "x/codex", "-b": "y/codex", "-c": "foo"}


# --------------------------------------------------------------------------
# B.1 degenerate-edge regression guards
# --------------------------------------------------------------------------
def test_same_cwd_members_degrade_without_raising() -> None:
    """Two distinct project dirs recording the SAME cwd have identical basename
    AND parent chain, so disambiguation cannot separate them. It must terminate
    at the max depth with a stable (still-colliding) label — not hang, not
    raise."""
    labels = _labels(
        [
            _summary("-a", ["/x/codex"]),
            _summary("-b", ["/x/codex"]),
        ]
    )
    # Both degrade to the full non-anchor path; still colliding, but stable and
    # exception-free (status-quo collision, not a manufactured one).
    assert labels == {"-a": "x/codex", "-b": "x/codex"}


def test_fallback_named_project_in_a_collision_is_excluded_not_crashed() -> None:
    """A project with no usable working dir (fallback name, best_path=None) that
    shares a display basename with a real-dir project must be EXCLUDED from
    disambiguation, never routed through it. The ``best is not None`` filter is
    load-bearing for CRASH-safety, not just labelling: without it,
    ``_path_suffix_label(None, ...)`` hits ``None.parts`` → AttributeError and
    takes down the whole index pass. Today nothing puts a fallback project
    through disambiguation, so this guards a latent crash.

    Mutation check (run manually, main/monk 6306/6307): drop ``if best is not
    None`` in ``_disambiguate_display_names`` → this test RAISES (AttributeError).
    If removing the filter leaves it green, it is pinning something else.
    """
    # "codex" (no leading dash, empty working dirs) → fallback display "codex",
    # best_path None. "-x-codex" with a real cwd → display "codex", best_path
    # /x/codex. Same display basename ⇒ same group.
    projects, _summary_obj = prepare_projects_index(
        [
            _summary("codex", []),  # fallback-named, best_path=None
            _summary("-x-codex", ["/x/codex"]),  # real-dir, best_path=/x/codex
        ]
    )
    labels = {p.name: p.display_name for p in projects}
    # No exception raised; the fallback keeps its decoded name and the real-dir
    # project is a singleton group (fallback excluded) → stays bare.
    assert labels == {"codex": "codex", "-x-codex": "codex"}


# --------------------------------------------------------------------------
# B.2 provider-aware title
# --------------------------------------------------------------------------
def test_title_defaults_to_claude_and_reflects_provider_label() -> None:
    summaries = [_summary("-a", ["/home/joe/alpha"])]
    # Default (Claude path) is byte-stable.
    assert title_for_projects_index(summaries).startswith("Claude Code Projects")
    # A provider label re-titles the page.
    codex = title_for_projects_index(summaries, provider_label="Codex")
    assert codex.startswith("Codex Projects")
    assert "Claude Code" not in codex

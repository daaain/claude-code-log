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

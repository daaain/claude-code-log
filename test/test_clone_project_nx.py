"""Tests for scripts/clone_project_nx.py's per-copy identifier rewriting.

The script exists to build oversized benchmark archives out of a real
project, which is only useful if every copy renders as an independent
project: shared identifiers would let dedup collapse messages across
copies, or point one copy's spawn entry at another copy's subagent.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "clone_project_nx.py"

_SESSION_UUID = "0123abcd-4567-89ef-0123-456789abcdef"
_AGENT_ID = "a82a709"  # Short hex, as real flat-layout sidecars use.
_TOOL_USE_ID = "toolu_01N46zUHxok8LnzqsfFc2eKq"
_PROSE = f"the file agent-{_AGENT_ID}.jsonl is discussed here"

_IN_BAND_RE = re.compile(r"agentId: (\S+) \(use SendMessage")


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("clone_project_nx", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clone_project_nx = _load_script()


def _run(src: Path, dst: Path, copies: int) -> None:
    old = sys.argv
    sys.argv = [str(_SCRIPT), str(src), str(dst), str(copies)]
    try:
        clone_project_nx.main()
    finally:
        sys.argv = old


def _write_project(src: Path) -> None:
    """A trunk session that spawns one subagent, plus the sidecar pair."""
    src.mkdir(parents=True, exist_ok=True)
    spawn_result = (
        "Done.\n\n"
        f"agentId: {_AGENT_ID} (use SendMessage with to: 'x' to continue this agent)\n"
        "<usage>total_tokens: 42</usage>"
    )
    trunk = [
        {
            "type": "user",
            "uuid": _SESSION_UUID,
            "sessionId": _SESSION_UUID,
            "requestId": "req_011CVabc",
            "message": {"content": [{"type": "text", "text": _PROSE}]},
        },
        {
            "type": "user",
            "uuid": "1123abcd-4567-89ef-0123-456789abcdef",
            "sessionId": _SESSION_UUID,
            "agentId": _AGENT_ID,
            "toolUseResult": {"agentId": _AGENT_ID},
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": _TOOL_USE_ID,
                        "content": spawn_result,
                    }
                ]
            },
        },
    ]
    (src / f"{_SESSION_UUID}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in trunk) + "\n", encoding="utf-8"
    )
    (src / f"agent-{_AGENT_ID}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "2123abcd-4567-89ef-0123-456789abcdef",
                "isSidechain": True,
                "agentId": _AGENT_ID,
                "message": {"content": [{"type": "text", "text": "on it"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (src / f"agent-{_AGENT_ID}.meta.json").write_text(
        json.dumps(
            {"agentType": "Explore", "toolUseId": _TOOL_USE_ID, "spawnDepth": 1}
        ),
        encoding="utf-8",
    )


def _trunk_files(project: Path) -> list[Path]:
    return sorted(f for f in project.glob("*.jsonl") if not f.name.startswith("agent-"))


def _entries(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def cloned(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write_project(src)
    _run(src, dst, 3)
    return dst


class TestSubagentIdentity:
    def test_sidecars_and_metas_are_unique_per_copy(self, cloned: Path):
        """No copy may overwrite an earlier copy's sidecar pair."""
        assert sorted(f.name for f in cloned.glob("agent-*.jsonl")) == [
            f"agent-{_AGENT_ID}.jsonl",
            f"agent-{_AGENT_ID}cp1.jsonl",
            f"agent-{_AGENT_ID}cp2.jsonl",
        ]
        assert sorted(f.name for f in cloned.glob("agent-*.meta.json")) == [
            f"agent-{_AGENT_ID}.meta.json",
            f"agent-{_AGENT_ID}cp1.meta.json",
            f"agent-{_AGENT_ID}cp2.meta.json",
        ]

    def test_in_band_agent_id_metadata_is_suffixed(self, cloned: Path):
        """The `agentId:` line in tool_result text follows the rename.

        Async spawns report their agent id in-band rather than as a JSON
        field (``factories/agent_metadata_factory.py`` parses it back
        out), so a copy whose in-band id still named the copy-0 sidecar
        would render a spawn link pointing into another copy.
        """
        in_band: list[str] = []
        for trunk in _trunk_files(cloned):
            result = _entries(trunk)[1]["message"]["content"][0]["content"]
            match = _IN_BAND_RE.search(result)
            assert match is not None, result
            in_band.append(match.group(1))

        assert sorted(in_band) == [_AGENT_ID, f"{_AGENT_ID}cp1", f"{_AGENT_ID}cp2"]
        # Each in-band id names a sidecar that actually exists.
        for agent_id in in_band:
            assert (cloned / f"agent-{agent_id}.jsonl").exists()

    def test_json_agent_id_fields_follow_the_rename(self, cloned: Path):
        for agent_file in sorted(cloned.glob("agent-*.jsonl")):
            expected = agent_file.name[len("agent-") : -len(".jsonl")]
            assert _entries(agent_file)[0]["agentId"] == expected

        # The trunk side too: field form and nested toolUseResult form.
        spawned = {_entries(trunk)[1]["agentId"] for trunk in _trunk_files(cloned)}
        nested = {
            _entries(trunk)[1]["toolUseResult"]["agentId"]
            for trunk in _trunk_files(cloned)
        }
        assert spawned == nested
        assert spawned == {_AGENT_ID, f"{_AGENT_ID}cp1", f"{_AGENT_ID}cp2"}

    def test_meta_tool_use_id_still_matches_its_copy(self, cloned: Path):
        """Each copy's meta links to that copy's spawning tool_result.

        The sidecar map is keyed by toolUseId across the whole directory,
        so copies sharing one id would collapse to a single entry and
        mislink every other copy's spawn.
        """
        seen: set[str] = set()
        for meta in sorted(cloned.glob("agent-*.meta.json")):
            tool_use_id = json.loads(meta.read_text(encoding="utf-8"))["toolUseId"]
            assert tool_use_id not in seen, "copies share a toolUseId"
            seen.add(tool_use_id)

            agent_id = meta.name[len("agent-") : -len(".meta.json")]
            owners = [
                trunk
                for trunk in _trunk_files(cloned)
                if _entries(trunk)[1]["message"]["content"][0]["tool_use_id"]
                == tool_use_id
            ]
            assert len(owners) == 1, f"{meta.name} has no unique spawning transcript"
            assert _entries(owners[0])[1]["agentId"] == agent_id

    def test_prose_mentions_are_left_alone(self, cloned: Path):
        """Only id-shaped references move, not text that mentions one."""
        for trunk in _trunk_files(cloned):
            assert _entries(trunk)[0]["message"]["content"][0]["text"] == _PROSE


class TestCopyIdentity:
    def test_copy_zero_is_byte_identical(self, cloned: Path, tmp_path: Path):
        src = tmp_path / "src"
        for f in sorted(src.iterdir()):
            assert (cloned / f.name).read_bytes() == f.read_bytes()

    def test_copies_past_sixteen_stay_distinct(self, tmp_path: Path):
        """A modulo-16 UUID rotation would wrap copy 16 onto copy 0."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write_project(src)
        _run(src, dst, 18)
        trunks = _trunk_files(dst)
        assert len(trunks) == 18
        assert len({_entries(f)[0]["sessionId"] for f in trunks}) == 18
        assert len(list(dst.glob("agent-*.jsonl"))) == 18
        assert len(list(dst.glob("agent-*.meta.json"))) == 18

    def test_request_and_tool_ids_are_per_copy(self, cloned: Path):
        trunks = _trunk_files(cloned)
        request_ids = {_entries(f)[0]["requestId"] for f in trunks}
        tool_ids = {
            _entries(f)[1]["message"]["content"][0]["tool_use_id"] for f in trunks
        }
        assert len(request_ids) == len(tool_ids) == 3


class TestDestinationGuards:
    def test_rejects_nonempty_directory(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write_project(src)
        dst.mkdir()
        (dst / "stray.txt").write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run(src, dst, 2)
        assert "not empty" in str(exc.value)

    def test_rejects_regular_file_destination(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write_project(src)
        dst.write_text("not a directory", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run(src, dst, 2)
        assert "not empty" in str(exc.value)

    def test_rejects_unmappable_basenames(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write_project(src)
        (src / "no-uuid-here.jsonl").write_text("{}\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run(src, dst, 2)
        assert "no-uuid-here.jsonl" in str(exc.value)
        assert not dst.exists(), "rejected before writing anything"

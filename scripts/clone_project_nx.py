#!/usr/bin/env python3
"""Clone a real project's transcripts N times with rewritten identifiers.

For benchmarking paths that only engage at scale (the render fan-out's
25k-message floor, pagination, the streaming pass) on a machine whose
real archives are too small: point it at a project, get one N times the
size whose every copy renders independently.

    uv run python scripts/clone_project_nx.py <source-project> <dest-dir> 4

Each copy c applies a bijective per-digit rotation to every UUID in each
file (uuid, parentUuid, sessionId, leafUuid, and the filenames
themselves) so copies never collide, and suffixes every
requestId so requestId-based dedup cannot collapse messages across
copies. Copy 0 is byte-identical to the source. Timestamps are left
alone, so all copies share the source's time range.

Subagent sidecars (``agent-<id>.jsonl``) usually carry a short hex id
rather than a UUID, so the rotation leaves their names untouched; those
get the same ``cp<c>`` suffix on both the filename and every ``"<id>"``
reference in the copied text, keeping each copy's spawn links pointing
at its own sidecar. Any other UUID-less basename is rejected up front,
since copies of it would overwrite one another.
"""

import re
import shutil
import sys
from pathlib import Path
from typing import Callable

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQ_RE = re.compile(r"req_[A-Za-z0-9]+")
AGENT_RE = re.compile(r"^agent-(.+)\.jsonl$")

HEX = "0123456789abcdef"

Translator = Callable[["re.Match[str]"], str]


def make_translators(c: int) -> tuple[Translator, Translator]:
    # The rotation offset for each hex position is that position's digit
    # of c in base 16, so every copy index gets its own offset vector: a
    # bijection within a copy (distinct UUIDs stay distinct) and distinct
    # across copies (no copy > 15 wraps back onto an earlier one, which a
    # single +c mod 16 rotation would). Copy 0 is the identity.
    def xlate_uuid(m: "re.Match[str]") -> str:
        out: list[str] = []
        pos = 0
        for ch in m.group(0):
            if ch == "-":
                out.append(ch)
                continue
            out.append(HEX[(HEX.index(ch) + ((c >> (4 * pos)) & 0xF)) % 16])
            pos += 1
        return "".join(out)

    def xlate_req(m: "re.Match[str]") -> str:
        return m.group(0) + f"cp{c}"

    return xlate_uuid, xlate_req


def make_agent_translator(c: int, agent_ids: list[str]) -> Callable[[str], str]:
    """Suffix every reference to a UUID-less subagent id with ``cp<c>``.

    The rotation can't make ``agent-a82a709.jsonl`` unique per copy — no
    UUID in the name — so those sidecars are renamed by suffix instead,
    and the ids inside the transcripts have to follow or the copy's spawn
    entries would point at another copy's sidecar. Only whole JSON string
    values are rewritten (``"agentId":"a82a709"``, ``"spawnedAgentId"``,
    id lists), never an id mentioned inside prose.
    """
    if not agent_ids:
        return lambda text: text
    pattern = re.compile('"(' + "|".join(re.escape(i) for i in agent_ids) + ')"')
    return lambda text: pattern.sub(lambda m: f'"{m.group(1)}cp{c}"', text)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <source-project> <dest-dir> <copies>")
    src = Path(sys.argv[1]).expanduser().resolve()
    dst = Path(sys.argv[2]).expanduser().resolve()
    copies = int(sys.argv[3])
    files = sorted(src.glob("*.jsonl"))
    if not files:
        sys.exit(f"No .jsonl transcripts in {src}")
    if copies < 1:
        sys.exit("copies must be at least 1")
    if dst == src or src in dst.parents:
        sys.exit("destination must be outside the source project")
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        sys.exit(f"destination {dst} exists and is not empty")

    # Names the rotation can't make unique: subagent sidecars get a
    # suffix instead (filename and ids together), anything else has no
    # safe mapping, so refuse rather than let copies overwrite silently.
    flat = [f.name for f in files if not UUID_RE.search(f.name)]
    agent_ids = sorted(
        m.group(1) for name in flat if (m := AGENT_RE.match(name)) is not None
    )
    unmappable = [name for name in flat if not AGENT_RE.match(name)]
    if copies > 1 and unmappable:
        sys.exit(
            "no UUID to rewrite in these filenames, so copies would "
            f"overwrite each other: {', '.join(sorted(unmappable)[:5])}"
        )

    dst.mkdir(parents=True, exist_ok=True)

    for c in range(copies):
        xlate_uuid, xlate_req = make_translators(c)
        xlate_agent = make_agent_translator(c, agent_ids)
        for f in files:
            name = UUID_RE.sub(xlate_uuid, f.name)
            if c and (m := AGENT_RE.match(name)) is not None and name == f.name:
                name = f"agent-{m.group(1)}cp{c}.jsonl"
            out = dst / name
            if c == 0:
                shutil.copyfile(f, out)
                continue
            text = f.read_text(encoding="utf-8")
            text = UUID_RE.sub(xlate_uuid, text)
            text = REQ_RE.sub(xlate_req, text)
            text = xlate_agent(text)
            out.write_text(text, encoding="utf-8")

    total = sum(f.stat().st_size for f in dst.glob("*.jsonl"))
    print(f"{len(list(dst.glob('*.jsonl')))} files, {total / 1e6:.0f}MB")


if __name__ == "__main__":
    main()

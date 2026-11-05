"""The dev box's config (foldyard.toml) against what the code needs of it.

The box's system packages are baked into an image layer from
``[box].packages`` instead of being installed by ``playwright install-deps
chromium`` on every fresh box. That call kept the list in step with the
Playwright version uv.lock pins by itself; this test is what keeps it in step
now: when a Playwright bump adds a library Chromium needs on Debian 13 (the
box's base), it fails, naming the packages to add.
"""

from __future__ import annotations

import re
from pathlib import Path

import playwright
import pytest

# Python 3.11+ (the CI matrix's newer legs run it); 3.10 has no tomllib.
tomllib = pytest.importorskip("tomllib")

REPO = Path(__file__).resolve().parents[1]
#: Where Playwright's per-distro package lists live. Since 1.64 the driver is
#: bundled, and they sit in coreBundle.js (before that, in
#: server/registry/nativeDeps.js). If a bump moves them again, this test fails
#: with FileNotFoundError or at the parse, and this path is what to update.
NATIVE_DEPS = Path(playwright.__path__[0]) / "driver/package/lib/coreBundle.js"
#: The box's base: foldyard's packaged generic box is Debian trixie.
DISTRO = "debian13"


def _box_packages() -> set[str]:
    config = tomllib.loads((REPO / "foldyard.toml").read_text(encoding="utf-8"))
    return set(config["box"]["packages"])


def _block(source: str, start: str) -> str:
    """The text of the object literal that opens at ``start``."""
    at = source.index(start)
    depth = 0
    for i in range(source.index("{", at), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[at : i + 1]
    raise AssertionError(f"unterminated block at {start!r}")


def _list(block: str, key: str) -> list[str]:
    match = re.search(rf"\b{key}: \[(.*?)\]", block, re.DOTALL)
    assert match, f"no {key} list"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_box_packages_cover_playwrights_chromium_deps() -> None:
    source = NATIVE_DEPS.read_text(encoding="utf-8")
    # The box runs on either architecture; arm64 must still borrow x64's
    # lists, or this test reads the wrong ones.
    arm = _block(source, f'deps["{DISTRO}-arm64"] =')
    for key in ("tools", "chromium"):
        assert f'...deps["{DISTRO}-x64"].{key}' in arm, (
            f"Playwright now lists {DISTRO}-arm64 {key} separately: read those too"
        )
    x64 = _block(source, f'"{DISTRO}-x64": {{')
    # What `playwright install-deps chromium` installs: tools + chromium.
    needed = set(_list(x64, "tools")) | set(_list(x64, "chromium"))
    assert len(needed) > 20, needed  # the parse found the lists
    missing = sorted(needed - _box_packages())
    assert not missing, (
        f"add to [box].packages in foldyard.toml (Playwright's {DISTRO} deps): {missing}"
    )

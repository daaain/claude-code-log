"""Keep the host's claude-code-log settings out of the test run.

A developer's shell may export settings the package reads — a theme, a
cache path, a render-pool size — and each changes what the code under
test does. `scrub_host_settings` removes them from ``os.environ``;
``conftest.py`` calls it before anything imports the package, because
some settings are read at import time (``renderer_timings``). A test
that needs one sets it with ``monkeypatch``, as it always had to on CI.

Generic host state (``TZ``, ``HOME``, colour and terminal width) is left
alone: setting each to unusual values changes no test's result, and
``HOME`` is also where Playwright keeps its browsers.

``test_host_env_isolation`` checks that every variable the package reads
is covered here: one outside the prefix must be added to
``_OTHER_SETTINGS``.

This module must not import ``claude_code_log``.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

_PREFIX = "CLAUDE_CODE_LOG_"
#: Settings the package reads that don't carry the prefix.
_OTHER_SETTINGS = frozenset({"CODEX_HOME"})


def is_host_setting(name: str) -> bool:
    """Whether *name* is a setting the package reads from the environment."""
    return name.startswith(_PREFIX) or name in _OTHER_SETTINGS


#: Settings the run fixes itself, after the scrub, to values of its own
#: (never the host's): the install key (``claude_code_log/page_key.py``),
#: so that pages carrying what it derives are the same in every test,
#: worker and snapshot. In the environment, not patched in, because
#: render workers are separate processes.
TEST_SETTINGS = {
    "CLAUDE_CODE_LOG_KEY_FILE": os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "test_data", "page-key.txt"
    ),
}


def scrub_host_settings(environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Remove every host setting from *environ*, then set the run's own
    (:data:`TEST_SETTINGS`); returns the names removed."""
    removed = sorted(name for name in environ if is_host_setting(name))
    for name in removed:
        del environ[name]
    environ.update(TEST_SETTINGS)
    return removed

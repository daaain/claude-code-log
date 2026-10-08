"""Tests for scripts/fmt_templates.py, which formats the template components
holding Jinja by masking it (`just fmt-js`, `just fmt-check`). Only the
masking is tested here: oxfmt is in the optional `js` group."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "fmt_templates.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("fmt_templates", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fmt_templates = _load_script()

_SOURCE = """{# A header comment
   on two lines. -#}
        (function () {
            'use strict';
{% include 'components/minimal/scheme.js' %}
            run();
        })();
"""


def test_mask_replaces_each_construct_by_one_placeholder_line():
    masked, blocks = fmt_templates.mask(_SOURCE)
    assert masked.splitlines()[0] == "/* jinja:0 */"
    assert masked.splitlines()[3] == "/* jinja:1 */"
    assert "{" + "%" not in masked and "{" + "#" not in masked
    assert blocks == [
        "{# A header comment\n   on two lines. -#}",
        "{% include 'components/minimal/scheme.js' %}",
    ]


def test_unmask_restores_verbatim_whatever_the_new_indent():
    masked, blocks = fmt_templates.mask(_SOURCE)
    # What a formatter does to the masked copy: dedent, re-indent the
    # placeholder inside the function.
    formatted = masked.replace("        ", "").replace(
        "/* jinja:1 */", "    /* jinja:1 */"
    )
    restored = fmt_templates.unmask(formatted, blocks)
    assert "\n{% include 'components/minimal/scheme.js' %}\n" in restored
    assert restored.startswith(blocks[0] + "\n")
    assert fmt_templates.unmask(masked, blocks) == _SOURCE


@pytest.mark.parametrize(
    "source",
    [
        "const theme = '{{ theme }}';\n",
        "{% if a %}{% include 'x.js' %}\n",
        "{% if a %}\n.x { color: red; } {# trailing #}\n",
    ],
)
def test_mask_refuses_jinja_sharing_a_line(source: str):
    with pytest.raises(fmt_templates.MaskError):
        fmt_templates.mask(source)


def test_unmask_refuses_a_lost_or_duplicated_placeholder():
    masked, blocks = fmt_templates.mask(_SOURCE)
    with pytest.raises(fmt_templates.MaskError):
        fmt_templates.unmask(masked.replace("/* jinja:1 */", ""), blocks)
    with pytest.raises(fmt_templates.MaskError):
        fmt_templates.unmask(masked + "/* jinja:0 */\n", blocks)


def test_every_jinja_component_masks_and_is_ignored_by_plain_oxfmt():
    files = fmt_templates.jinja_components()
    assert files, "no component holds Jinja: is the scan looking in the wrong place?"
    ignored = fmt_templates.read_config()["ignorePatterns"]
    for f in files:
        source = (fmt_templates.REPO / f).read_text(encoding="utf-8")
        masked, blocks = fmt_templates.mask(source)
        assert blocks, f
        assert fmt_templates.unmask(masked, blocks) == source, f
        assert f in ignored, f"{f} holds Jinja but plain oxfmt would parse it"

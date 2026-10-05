"""Generate live documentation pages at MkDocs build time.

Run by the ``mkdocs-gen-files`` plugin (see ``mkdocs.yml``). It introspects the
TUI to produce an always-current reference page:

* TUI screenshots (SVG), captured by driving the real screens headlessly.
* A keybindings table per screen, read from the Textual ``BINDINGS``.

Screenshot capture is wrapped in error handling: if it fails (e.g. in a
constrained CI runner) the page still builds, just without images, so prose
docs are never blocked by a screenshot hiccup.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import mkdocs_gen_files

# Make the standalone generator scripts importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from generate_tui_docs import build_keybindings_markdown  # noqa: E402
from generate_tui_screenshots import Screenshot, generate_screenshots  # noqa: E402
from generate_example_output import generate_example_html  # noqa: E402


def _emit_example_output(theme: str = "classic") -> bool:
    """Render the bundled sample project into the site at examples/.

    The classic page is ``examples/transcript.html``, the minimal theme's
    ``examples/transcript-minimal.html``. Returns True on success. Failures
    are swallowed (with a warning) so a render hiccup can never block the
    rest of the docs build.
    """
    name = "transcript.html" if theme == "classic" else f"transcript-{theme}.html"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            html = generate_example_html(Path(tmp) / "transcript.html", theme)
            data = html.read_bytes()
        with mkdocs_gen_files.open(f"examples/{name}", "wb") as fh:
            fh.write(data)
        return True
    except Exception as exc:  # noqa: BLE001 - never let the example break docs
        print(
            f"WARNING: example output generation ({theme}) failed: {exc}",
            file=sys.stderr,
        )
        return False


def _emit_screenshots() -> list[Screenshot]:
    """Capture TUI screenshots into the virtual site under assets/tui/."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        shots = generate_screenshots(out_dir)
        for shot in shots:
            data = (out_dir / shot.filename).read_bytes()
            with mkdocs_gen_files.open(f"assets/tui/{shot.filename}", "wb") as fh:
                fh.write(data)
        return shots


def _build_page() -> str:
    parts: list[str] = [
        "# TUI Reference",
        "",
        (
            "The interactive terminal UI (`claude-code-log --tui`) lets you "
            "browse, export, and resume sessions. The screenshots and "
            "keybinding tables below are generated from the running TUI at "
            "build time, so they stay in sync with the shipped interface."
        ),
        "",
        "## Screenshots",
        "",
    ]

    try:
        shots = _emit_screenshots()
    except Exception as exc:  # noqa: BLE001 - never let a screenshot break docs
        print(f"WARNING: TUI screenshot generation failed: {exc}", file=sys.stderr)
        parts.append(
            "_Screenshots could not be generated in this build environment. "
            "Run `just docs-gen` locally to preview them._"
        )
        parts.append("")
    else:
        for shot in shots:
            parts.append(f"### {shot.title}")
            parts.append("")
            parts.append(f"![{shot.title}](../assets/tui/{shot.filename})")
            parts.append("")
            parts.append(f"*{shot.caption}*")
            parts.append("")

    # Append the keybindings reference (drop its top-level H1; this page owns it).
    keybindings = build_keybindings_markdown()
    keybindings = keybindings.split("\n", 1)[1].lstrip("\n")
    parts.append("## Keybindings")
    parts.append("")
    parts.append(keybindings)

    return "\n".join(parts).rstrip() + "\n"


with mkdocs_gen_files.open("reference/tui.md", "w") as fh:
    fh.write(_build_page())


def _build_example_page(generated: bool, minimal: bool = False) -> str:
    parts = [
        "# Example output",
        "",
        (
            "This is a real, self-contained page produced by `claude-code-log`, "
            "rendered from a sample of this project's own early development "
            "(23 sessions) that ships in the repo. It is regenerated on every "
            "docs build, so it always reflects the current rendering."
        ),
        "",
    ]
    if generated:
        parts += [
            "[Open the full example transcript]"
            "(examples/transcript.html){ .md-button target=_blank }",
        ]
        if minimal:
            parts += [
                "[Open the example in the minimal theme]"
                "(examples/transcript-minimal.html){ .md-button target=_blank }",
                "",
                "The minimal page is the same transcript rendered with "
                "`--theme minimal` — see [Themes](themes.md).",
            ]
        parts += [
            "",
            '<iframe src="../examples/transcript.html" title="Example transcript" '
            'style="width:100%;height:80vh;border:1px solid var(--md-default-fg-color--lightest);'
            'border-radius:4px;margin-top:1rem;"></iframe>',
        ]
    else:
        parts.append(
            "_The example could not be generated in this build environment. "
            "Run `python scripts/generate_example_output.py` locally to preview it._"
        )
    return "\n".join(parts).rstrip() + "\n"


_example_generated = _emit_example_output()
_minimal_generated = _example_generated and _emit_example_output("minimal")
with mkdocs_gen_files.open("example.md", "w") as fh:
    fh.write(_build_example_page(_example_generated, _minimal_generated))

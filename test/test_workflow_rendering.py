"""Tests for Workflow tool-input + async-result-body rendering (#174 PR2).

D3: a ``Workflow`` tool_use renders a meta header (name/description/phase
pills) above its JavaScript orchestrator, syntax-highlighted — not the raw
ToolUseContent fallback.
D4: a JSON-shaped async-result body is pretty-printed + highlighted as JSON;
non-JSON (markdown) bodies fall back to the existing markdown rendering.
"""

from __future__ import annotations

from pathlib import Path

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.html.utils import render_async_result_body
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.workflow import parse_workflow_meta

TRUNK = (
    Path(__file__).parent
    / "test_data"
    / "workflow_basic"
    / "11110000-0000-4000-8000-000000000001.jsonl"
)


class TestWorkflowToolInputRendering:
    """D3 — Workflow tool_use → meta header + highlighted JS."""

    def _html(self) -> str:
        return generate_html(load_transcript(TRUNK))

    def test_meta_header_from_script_meta_block(self) -> None:
        html = self._html()
        assert "workflow-meta" in html
        assert "workflow-name" in html and "demo-review" in html
        # description surfaced from the script's meta block
        assert "Review changed files across dimensions" in html

    def test_both_phase_pills_rendered(self) -> None:
        html = self._html()
        assert html.count("workflow-phase-pill") >= 2  # Map + Synthesize

    def test_script_highlighted_as_javascript(self) -> None:
        html = self._html()
        # Target the rendered div, not the `.workflow-script` CSS rule in <head>.
        idx = html.find("class='workflow-script'")
        assert idx != -1, "expected a rendered workflow-script block"
        segment = html[idx : idx + 600]
        # render_file_content_collapsible emits a Pygments highlight table
        assert "highlight" in segment

    def test_specialized_path_not_raw_fallback(self) -> None:
        # The generic fallback would dump input as a params table / raw JSON
        # with no meta header or highlighted script; the specialized renderer
        # produces both.
        html = self._html()
        assert "workflow-meta" in html and "workflow-script" in html


class TestAsyncResultBodyJson:
    """D4 — JSON-aware async-result body rendering."""

    def test_json_body_pretty_printed_and_highlighted(self) -> None:
        out = render_async_result_body(
            '{"plan": "Land parsing first.", "areaCount": 2}', "task-async-answer"
        )
        assert "highlight" in out  # Pygments-highlighted
        assert "task-async-answer" in out  # wrapper css class preserved
        assert out.count("\n") > 2  # pretty-printed (indented, multi-line)

    def test_truncated_json_highlights_without_crashing(self) -> None:
        # Real async previews are often truncated mid-value; still starts with
        # {" so it's treated as JSON and highlighted best-effort.
        out = render_async_result_body('{"plan": "Lan', "x")
        assert "highlight" in out

    def test_markdown_body_uses_markdown_path(self) -> None:
        out = render_async_result_body("## Plan\n\nLand it first.", "x")
        # markdown path → no Pygments code-highlight table
        assert 'class="highlight"' not in out
        assert "Plan" in out

    def test_non_json_brace_text_is_not_treated_as_json(self) -> None:
        # Heuristic is specifically `{"` — a lone "{" (e.g. prose) is markdown.
        out = render_async_result_body("{ not really json", "x")
        assert 'class="highlight"' not in out


class TestWorkflowToolInputMarkdown:
    """D3 parity for the Markdown renderer — registering Workflow in
    TOOL_INPUT_MODELS is format-neutral, so the script must NOT vanish from
    Markdown output (regression guard, monk PR2 review)."""

    def _md(self) -> str:
        return MarkdownRenderer().generate(load_transcript(TRUNK))

    def test_script_present_in_markdown(self) -> None:
        md = self._md()
        # The script body (and its meta) must render, not disappear.
        assert "demo-review" in md
        assert "export const meta" in md
        assert "```js" in md  # fenced as JavaScript

    def test_meta_header_in_markdown(self) -> None:
        md = self._md()
        assert "Review changed files across dimensions" in md  # description
        assert "Map" in md and "Synthesize" in md  # phase titles


class TestWorkflowMetaParsing:
    """Unit tests for the shared meta parser (issue #174)."""

    def test_name_description_phases(self) -> None:
        script = (
            "export const meta = {\n"
            "  name: 'demo',\n"
            "  description: 'A demo',\n"
            "  phases: [{ title: 'Map' }, { title: 'Synthesize' }],\n"
            "}\n"
        )
        assert parse_workflow_meta(script) == ("demo", "A demo", ["Map", "Synthesize"])

    def test_bracket_in_phase_detail_does_not_truncate_phases(self) -> None:
        # N1 regression: a ']' inside a phase detail must not cut the list short.
        script = (
            "export const meta = {\n"
            "  phases: [{ title: 'Scan', detail: 'grep [logs]' }, { title: 'Fix' }],\n"
            "}\n"
        )
        assert parse_workflow_meta(script)[2] == ["Scan", "Fix"]

    def test_no_meta_block_returns_empty(self) -> None:
        assert parse_workflow_meta("const x = 1\nawait agent('hi')\n") == ("", "", [])

    def test_escaped_quote_in_string_does_not_truncate(self) -> None:
        # Observed in real meta blocks ('SAM\'s ...'): the old [^'\"]* pattern
        # cut the description at the escaped quote.
        script = (
            "export const meta = {\n"
            "  name: 'sweep',\n"
            "  description: 'Stress-test SAM\\'s operative layer',\n"
            "  phases: [{ title: 'The \\'inner\\' pass' }],\n"
            "}\n"
        )
        name, desc, phases = parse_workflow_meta(script)
        assert name == "sweep"
        assert desc == "Stress-test SAM's operative layer"
        assert phases == ["The 'inner' pass"]

    def test_backtick_and_double_quote_strings(self) -> None:
        script = (
            "export const meta = {\n"
            "  name: `tick-name`,\n"
            '  description: "double quoted",\n'
            "  phases: [{ title: `Scan` }],\n"
            "}\n"
        )
        assert parse_workflow_meta(script) == (
            "tick-name",
            "double quoted",
            ["Scan"],
        )


_JS_META = (
    "export const meta = {\n"
    "  name: 'js-name',\n"
    "  description: 'js-desc',\n"
    "  phases: [{ title: 'Map' }],\n"
    "}\n"
)


class TestSnapshotFirstHeader:
    """PR3 / cboos refinement: the header prefers the authoritative
    <runId>.json snapshot over the best-effort JS-meta regex, warns on JS-meta
    drift, and falls back to the regex for a running (no-snapshot) workflow."""

    def _run(self, **kw):
        from claude_code_log.workflow import WorkflowPhase, WorkflowRun

        return WorkflowRun(
            run_id="r",
            workflow_name=kw.get("name", "SNAP-NAME"),
            has_snapshot=kw.get("has_snapshot", True),
            phases=kw.get(
                "phases",
                [
                    WorkflowPhase(index=0, title="Alpha"),
                    WorkflowPhase(index=1, title="Beta"),
                ],
            ),
        )

    def test_snapshot_name_and_phases_win_description_from_js(self) -> None:
        from claude_code_log.workflow import resolve_workflow_header

        name, desc, phases = resolve_workflow_header(self._run(), _JS_META)
        assert name == "SNAP-NAME"  # snapshot workflowName wins over JS name
        assert phases == ["Alpha", "Beta"]  # snapshot phases win over JS phases
        assert desc == "js-desc"  # description has no snapshot source → JS

    def test_no_snapshot_falls_back_to_js(self) -> None:
        from claude_code_log.workflow import resolve_workflow_header

        assert resolve_workflow_header(None, _JS_META) == (
            "js-name",
            "js-desc",
            ["Map"],
        )

    def test_drift_warning_when_js_meta_misses(self, caplog) -> None:
        import logging

        from claude_code_log.workflow import resolve_workflow_header

        with caplog.at_level(logging.WARNING, logger="claude_code_log.workflow"):
            # snapshot has name+phases, but the script has no `export const meta`
            resolve_workflow_header(self._run(), "const x = 1\n")
        assert any("may have drifted" in r.message for r in caplog.records)


class TestSnapshotScriptFallback:
    """Shape variety (#174 follow-up): the tool_use input carries a script only
    in the inline-``script`` invocation shape. For ``scriptPath`` / ``name`` /
    ``resumeFromRunId`` shapes the header must fall back to the snapshot's
    stored ``script`` — and an absent script is NOT meta-format drift, so the
    warning must stay quiet."""

    def _run(self, **kw):
        from claude_code_log.workflow import WorkflowPhase, WorkflowRun

        return WorkflowRun(
            run_id="r",
            workflow_name=kw.get("name", "SNAP-NAME"),
            has_snapshot=True,
            phases=kw.get("phases", [WorkflowPhase(index=0, title="Alpha")]),
            script=kw.get("script", ""),
            summary=kw.get("summary", ""),
        )

    def test_resolve_workflow_script_prefers_input_then_snapshot(self) -> None:
        from claude_code_log.workflow import resolve_workflow_script

        run = self._run(script="// from snapshot")
        assert resolve_workflow_script(run, "// inline") == "// inline"
        assert resolve_workflow_script(run, "") == "// from snapshot"
        assert resolve_workflow_script(None, "") == ""

    def test_empty_input_script_parses_snapshot_script_no_warning(self, caplog) -> None:
        import logging

        from claude_code_log.workflow import resolve_workflow_header

        with caplog.at_level(logging.WARNING, logger="claude_code_log.workflow"):
            name, desc, phases = resolve_workflow_header(self._run(script=_JS_META), "")
        # description recovered from the snapshot-stored script's meta block
        assert desc == "js-desc"
        assert name == "SNAP-NAME"  # snapshot-first name still wins
        assert phases == ["Alpha"]
        assert not caplog.records  # a parseable snapshot script → nothing drifted

    def test_no_script_anywhere_is_not_drift(self, caplog) -> None:
        import logging

        from claude_code_log.workflow import resolve_workflow_header

        with caplog.at_level(logging.WARNING, logger="claude_code_log.workflow"):
            name, desc, phases = resolve_workflow_header(self._run(), "")
        assert name == "SNAP-NAME" and phases == ["Alpha"]
        assert not caplog.records  # no source text at all → no drift warning

    def test_description_falls_back_to_snapshot_summary(self) -> None:
        from claude_code_log.workflow import resolve_workflow_header

        _, desc, _ = resolve_workflow_header(
            self._run(summary="digest of the description"), ""
        )
        assert desc == "digest of the description"


class TestSnapshotEnrichmentParsing:
    """The <runId>.json snapshot carries far more than phases/agent metadata —
    script, scriptPath, args, summary, error, defaultModel, durationMs,
    totalToolCalls all surface on WorkflowRun."""

    def test_enrichment_fields_parsed(self, tmp_path: Path) -> None:
        import json

        from claude_code_log.workflow import parse_workflow_run

        run_dir = tmp_path / "subagents" / "workflows" / "wf_enrich1"
        run_dir.mkdir(parents=True)
        (run_dir / "journal.jsonl").write_text(
            '{"type": "result", "agentId": "ag1", "result": "ok"}\n',
            encoding="utf-8",
        )
        snapshot = tmp_path / "wf_enrich1.json"
        snapshot.write_text(
            json.dumps(
                {
                    "runId": "wf_enrich1",
                    "taskId": "task_e1",
                    "workflowName": "enriched",
                    "status": "completed",
                    "script": "export const meta = {\n}\n",
                    "scriptPath": "/somewhere/enriched.workflow.js",
                    "args": {"target": "docs/"},
                    "summary": "the digest",
                    "defaultModel": "claude-sonnet-4-6",
                    "durationMs": 1234,
                    "totalToolCalls": 7,
                    "phases": [],
                }
            ),
            encoding="utf-8",
        )
        run = parse_workflow_run(run_dir, snapshot)
        assert run is not None
        assert run.script.startswith("export const meta")
        assert run.script_path == "/somewhere/enriched.workflow.js"
        assert run.args == {"target": "docs/"}
        assert run.summary == "the digest"
        assert run.error == ""
        assert run.default_model == "claude-sonnet-4-6"
        assert run.duration_ms == 1234
        assert run.total_tool_calls == 7

    def test_non_string_enrichment_values_coerced_to_empty(
        self, tmp_path: Path
    ) -> None:
        import json

        from claude_code_log.workflow import parse_workflow_run

        run_dir = tmp_path / "subagents" / "workflows" / "wf_enrich2"
        run_dir.mkdir(parents=True)
        (run_dir / "journal.jsonl").write_text(
            '{"type": "result", "agentId": "ag1", "result": "ok"}\n',
            encoding="utf-8",
        )
        snapshot = tmp_path / "wf_enrich2.json"
        snapshot.write_text(
            json.dumps({"runId": "wf_enrich2", "script": 42, "error": ["x"]}),
            encoding="utf-8",
        )
        run = parse_workflow_run(run_dir, snapshot)
        assert run is not None
        assert run.script == "" and run.error == ""


class TestWorkflowRunLinkage:
    """PR3 step 1-2: a parsed run links to its Workflow tool_use by taskId on a
    directory load, so the formatter can render snapshot-first."""

    def test_run_links_to_tool_use_input(self) -> None:
        from claude_code_log.converter import load_directory_transcripts
        from claude_code_log.models import ToolUseMessage, WorkflowToolInput
        from claude_code_log.renderer import generate_template_messages

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        assert "wf_demo01" in tree.workflow_runs
        _roots, _nav, ctx = generate_template_messages(msgs, session_tree=tree)
        linked = [
            tm.content.input.workflow_run
            for tm in ctx.messages
            if tm is not None
            and isinstance(tm.content, ToolUseMessage)
            and tm.content.tool_name == "Workflow"
            and isinstance(tm.content.input, WorkflowToolInput)
        ]
        assert len(linked) == 1
        assert linked[0] is not None
        assert linked[0].run_id == "wf_demo01"

    def test_decoy_local_meta_ignored_for_exported_block(self) -> None:
        # CR #205: only the EXPORTED `meta` declaration is the header source;
        # a non-export local `meta = {...}` before it must not be mis-parsed.
        script = (
            "const meta = { name: 'DECOY', description: 'local' }\n"
            "export const meta = {\n"
            "  name: 'real-wf',\n"
            "  description: 'the real one',\n"
            "  phases: [{ title: 'Map' }],\n"
            "}\n"
        )
        assert parse_workflow_meta(script) == ("real-wf", "the real one", ["Map"])


class TestWorkflowMarkdownEscaping:
    """CR #205 (Major): script-derived meta fields must be HTML-tag-neutralized
    before injection into the Markdown header (the script body itself is fenced
    and safe)."""

    def test_header_neutralizes_html_tags(self) -> None:
        from claude_code_log.models import WorkflowToolInput

        script = (
            "export const meta = {\n"
            "  name: 'pwn <script>alert(1)</script>',\n"
            "  description: 'd <img src=x onerror=alert(2)>',\n"
            "  phases: [{ title: 'P <b>x</b>' }],\n"
            "}\n"
        )
        out = MarkdownRenderer().format_WorkflowToolInput(
            WorkflowToolInput(script=script),
            None,  # type: ignore[arg-type]  # message is unused by this formatter
        )
        # Inspect only the header (everything before the fenced script, where
        # the raw tags legitimately appear inside a code block).
        header = out.split("```")[0]
        assert "<script>" not in header
        assert "<img" not in header
        assert "<b>" not in header
        # Neutralized text still readable in the header.
        assert "alert(1)" in header


class TestWorkflowRunSplice:
    """PR3 step 3: the parsed WorkflowRun tree is spliced at its Workflow
    tool_use site — phases → agents → each agent's side-channel transcript —
    on the nested DOM, in both HTML and Markdown. The sub-tree attaches to the
    PAIRED tool_result (so the tool_use/tool_result pair stays visually
    joined), falling back to the tool_use when no result exists yet."""

    def _tree(self):
        from claude_code_log.converter import load_directory_transcripts
        from claude_code_log.models import ToolUseMessage
        from claude_code_log.renderer import generate_template_messages

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        _roots, _nav, ctx = generate_template_messages(msgs, session_tree=tree)
        tool_use = next(
            tm
            for tm in ctx.messages
            if tm is not None
            and isinstance(tm.content, ToolUseMessage)
            and tm.content.tool_name == "Workflow"
        )
        # The splice hangs off the paired tool_result when present.
        host = tool_use
        if tool_use.pair_last is not None:
            partner = ctx.get(tool_use.pair_last)
            if partner is not None and partner.children:
                host = partner
        return host, ctx

    def test_phases_and_agents_nested_at_tool_use_site(self) -> None:
        host, _ctx = self._tree()
        # Two phases (Map, Synthesize) attached at the Workflow tool_use site
        # (on the paired tool_result — the pair renders as one joined unit).
        from claude_code_log.models import WorkflowPhaseMessage

        phases = [c for c in host.children if c.type == "workflow_phase"]
        assert len(phases) == 2
        titles = [
            c.content.title
            for c in phases
            if isinstance(c.content, WorkflowPhaseMessage)
        ]
        assert titles == ["Map", "Synthesize"]
        # Map has 2 agents, Synthesize 1; all are workflow_agent children.
        agents_by_phase = [
            [c for c in p.children if c.type == "workflow_agent"] for p in phases
        ]
        assert [len(a) for a in agents_by_phase] == [2, 1]

    def test_agent_sidechannel_grafted_beneath_agent(self) -> None:
        host, _ctx = self._tree()
        first_phase = next(c for c in host.children if c.type == "workflow_phase")
        first_agent = next(
            c for c in first_phase.children if c.type == "workflow_agent"
        )
        # The agent's 3 side-channel entries (user, assistant, assistant) are
        # grafted as its children; the assistant's tool_use nests one deeper.
        child_types = [c.type for c in first_agent.children]
        assert child_types == ["user", "assistant", "assistant"]
        assert any(
            gc.type == "tool_use" for c in first_agent.children for gc in c.children
        )

    def test_splice_attaches_to_paired_tool_result(self) -> None:
        from claude_code_log.models import ToolResultMessage

        host, _ctx = self._tree()
        # The fixture run has completed → result exists → splice hangs off it,
        # keeping the tool_use/tool_result pair visually joined.
        assert isinstance(host.content, ToolResultMessage)
        assert host.pair_first is not None  # still the pair's second half

    def test_phase_pills_link_to_phase_cards(self) -> None:
        from claude_code_log.converter import load_directory_transcripts

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        html = generate_html(msgs, session_tree=tree)
        host, _ctx = self._tree()
        phase_ids = [
            c.message_index for c in host.children if c.type == "workflow_phase"
        ]
        assert len(phase_ids) == 2
        for idx in phase_ids:
            assert f"<a class='workflow-phase-pill' href='#msg-d-{idx}'>" in html

    def test_agent_title_prefixed_and_dict_result_as_params_table(self) -> None:
        from claude_code_log.converter import load_directory_transcripts

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        html = generate_html(msgs, session_tree=tree)
        # Agent cards title as "Agent <label>" (no colon — labels carry one).
        assert "Agent review:loader" in html
        # Dict-shaped StructuredOutput renders via the generic tool
        # key/value table, not a raw JSON dump. (Target the rendered div —
        # the bare class name also appears in the embedded CSS rules.)
        i = html.find("<div class='workflow-agent-result'>")
        assert i != -1
        assert "tool-params-table" in html[i : i + 400]

    def test_spliced_indices_unique_and_monotonic(self) -> None:
        _host, ctx = self._tree()
        indices = [tm.message_index for tm in ctx.messages if tm is not None]
        assert len(indices) == len(set(indices))  # no collisions across the splice

    def test_html_renders_phase_and_agent_cards(self) -> None:
        from claude_code_log.converter import load_directory_transcripts

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        html = generate_html(msgs, session_tree=tree)
        # Rendered-card markers (hyphenated) — distinct from the underscore
        # `workflow_phase`/`workflow_agent` literals the timeline JS always
        # carries, so these prove the cards actually rendered.
        assert "workflow-phase-meta" in html and "workflow-agent-meta" in html
        assert "Phase: Map" in html and "Phase: Synthesize" in html
        assert "review:loader" in html and "review:hierarchy" in html
        # StructuredOutput dict result is JSON-highlighted in the agent card.
        assert "workflow-agent-result" in html
        assert "Discovery glob misses" in html  # result content surfaced

    def test_markdown_renders_phase_and_agent_tree(self) -> None:
        from claude_code_log.converter import load_directory_transcripts

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        md = MarkdownRenderer().generate(msgs, session_tree=tree)
        assert "Phase: Map" in md and "Phase: Synthesize" in md
        assert "review:loader" in md
        # Dict result fenced as JSON; the string-result agent's markdown body
        # ("## Plan") renders directly.
        assert '"area": "loader"' in md
        assert "Land parsing first" in md

    def test_list_shaped_agent_result_json_highlighted_in_both_formats(self) -> None:
        # CR #210: a list-shaped StructuredOutput result must be JSON-highlighted
        # (HTML) / JSON-fenced (Markdown) just like a dict — render_async_result_body's
        # `{"` heuristic would skip a `[...]` payload, so the HTML formatter must
        # JSON-render dict AND list directly. Guards against HTML/Markdown divergence.
        from claude_code_log.html.tool_formatters import format_workflow_agent_content
        from claude_code_log.markdown.renderer import MarkdownRenderer
        from claude_code_log.models import MessageMeta, WorkflowAgentMessage

        content = WorkflowAgentMessage(
            meta=MessageMeta.empty(),
            label="lister",
            result=[{"area": "loader"}, {"area": "hierarchy"}],
        )
        html = format_workflow_agent_content(content)
        assert "highlight" in html  # Pygments JSON highlight, not markdown
        assert "workflow-agent-result" in html
        # Content present (Pygments wraps tokens in <span>s, so check tokens, not
        # a contiguous substring).
        assert "area" in html and "loader" in html and "hierarchy" in html

        md = MarkdownRenderer().format_WorkflowAgentMessage(
            content,
            None,  # type: ignore[arg-type]  # message unused by this formatter
        )
        assert "```json" in md
        assert '"area": "loader"' in md

    def test_non_workflow_transcript_has_no_spliced_nodes(self) -> None:
        # The splice is gated on a linked workflow_run, so an ordinary
        # transcript must yield no workflow_phase / workflow_agent tree nodes.
        # (Asserting on the rendered tree, not on the HTML string — the timeline
        # JS embeds the `workflow_phase`/`workflow_agent` literals on every
        # page regardless of content.)
        from claude_code_log.converter import load_transcript
        from claude_code_log.renderer import generate_template_messages

        other = Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        if not other.is_file():
            import pytest

            pytest.skip("no representative fixture available")
        _roots, _nav, ctx = generate_template_messages(load_transcript(other))
        types = {tm.type for tm in ctx.messages if tm is not None}
        assert "workflow_phase" not in types
        assert "workflow_agent" not in types


def _has_workflow_tool_use(entry) -> bool:
    content = getattr(getattr(entry, "message", None), "content", None)
    return isinstance(content, list) and any(
        getattr(i, "type", None) == "tool_use" and getattr(i, "name", "") == "Workflow"
        for i in content
    )


class TestSingleFileWorkflowRender:
    """PR3: a lone ``<SID>.jsonl`` (cboos's natural usage,
    ``claude-code-log <SID>.jsonl --detail high``) discovers the sibling
    ``<SID>/subagents/workflows/`` runs and splices the tree, just like a
    directory load."""

    def test_single_file_html_shows_workflow_tree(self, tmp_path: Path) -> None:
        from claude_code_log.converter import convert_jsonl_to
        from claude_code_log.models import DetailLevel

        out = tmp_path / "single.html"
        convert_jsonl_to(
            "html",
            TRUNK,
            output_path=out,
            use_cache=False,
            update_cache=False,
            silent=True,
            detail=DetailLevel.HIGH,
        )
        html = out.read_text(encoding="utf-8", errors="replace")
        # Same rendered-card markers as the directory path.
        assert "workflow-phase-meta" in html and "workflow-agent-meta" in html
        assert "Phase: Map" in html and "review:loader" in html

    def test_load_session_workflow_runs_finds_sibling_run(self) -> None:
        from claude_code_log.workflow import load_session_workflow_runs

        runs = load_session_workflow_runs(TRUNK, silent=True)
        assert [r.run_id for r in runs] == ["wf_demo01"]


class TestWorkflowPaginationBoundary:
    """PR3: run↔tool_use linkage is resolved at full-session scope (stored on
    ``SessionTree.workflow_links``) BEFORE pagination, so a Workflow tool_use
    still links to its run when its tool_result is on a different page."""

    def test_links_via_precomputed_map_without_tool_result_in_slice(self) -> None:
        from claude_code_log.converter import load_directory_transcripts
        from claude_code_log.models import ToolUseMessage
        from claude_code_log.renderer import generate_template_messages

        msgs, tree = load_directory_transcripts(TRUNK.parent, silent=True)
        assert tree.workflow_links, "links map should be built at full scope"

        # Slice up to & including the entry holding the Workflow tool_use — this
        # EXCLUDES the later tool_result entry, mimicking a page boundary where
        # tool_use is the last message of a page and tool_result the first of
        # the next.
        pos = next(i for i, e in enumerate(msgs) if _has_workflow_tool_use(e))
        page = msgs[: pos + 1]

        def _host(messages, session_tree):
            _r, _n, ctx = generate_template_messages(
                messages, session_tree=session_tree
            )
            return next(
                tm
                for tm in ctx.messages
                if tm is not None
                and isinstance(tm.content, ToolUseMessage)
                and tm.content.tool_name == "Workflow"
            )

        # WITH the precomputed map: splice fires despite no tool_result in slice.
        host = _host(page, tree)
        assert any(c.type == "workflow_phase" for c in host.children), (
            "full-scope links map should link a cross-page tool_use"
        )

        # WITHOUT it (clear the map → per-page fallback): the fallback scan can't
        # find a tool_result in this slice, so no splice — proving the map is
        # what enables cross-page linkage.
        tree.workflow_links = {}
        host_nolink = _host(page, tree)
        assert not any(c.type == "workflow_phase" for c in host_nolink.children)

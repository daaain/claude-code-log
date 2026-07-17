"""Tree-sitter parsing and bounded Codex JavaScript analysis."""

from claude_code_log.providers.codex_javascript import parse_javascript


def test_parse_javascript_retains_exact_utf8_node_ranges() -> None:
    syntax = parse_javascript('const subject = "Ready 😎";')

    assert syntax is not None
    declaration = syntax.root.named_children[0]
    declarator = declaration.named_children[0]
    value = declarator.child_by_field_name("value")
    assert value is not None
    assert syntax.text(value) == '"Ready 😎"'


def test_parse_javascript_rejects_recovered_syntax() -> None:
    assert parse_javascript("const result = await tools.exec_command({") is None


def test_parse_javascript_enforces_source_and_node_limits() -> None:
    assert parse_javascript("const value = 1;", max_source_bytes=5) is None
    assert parse_javascript("const value = 1;", max_syntax_nodes=2) is None

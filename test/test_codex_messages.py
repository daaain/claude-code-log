"""Structured Codex user-message formatting."""

from claude_code_log.providers.codex_messages import format_codex_user_message


ENVIRONMENT_CONTEXT = """<environment_context>
  <cwd>/workspace/synthetic-project</cwd>
  <shell>bash</shell>
  <current_date>2026-07-10</current_date>
  <timezone>Europe/Paris</timezone>
  <filesystem>
    <workspace_roots><root>/workspace/synthetic-project</root></workspace_roots>
    <permission_profile type="managed">
      <file_system type="restricted">
        <entry access="read"><special>:root</special></entry>
        <entry access="write"><path>/workspace/synthetic-project</path></entry>
        <entry access="write"><special>:slash_tmp</special></entry>
      </file_system>
    </permission_profile>
  </filesystem>
</environment_context>"""


def test_environment_context_becomes_markdown_tables() -> None:
    result = format_codex_user_message(ENVIRONMENT_CONTEXT)

    assert result.startswith("### Environment context")
    assert "| Working directory | `/workspace/synthetic-project` |" in result
    assert "| Shell | `bash` |" in result
    assert "| Current date | `2026-07-10` |" in result
    assert "| Timezone | `Europe/Paris` |" in result
    assert "#### Workspace roots" in result
    assert "- `/workspace/synthetic-project`" in result
    assert "Profile `managed`; filesystem `restricted`." in result
    assert "| `read` | `special:root` |" in result
    assert "| `write` | `special:slash_tmp` |" in result
    assert "<environment_context>" not in result


def test_ordinary_and_malformed_user_messages_are_unchanged() -> None:
    ordinary = "Please inspect <environment_context> examples."
    malformed = "<environment_context><cwd>/synthetic"

    assert format_codex_user_message(ordinary) == ordinary
    assert format_codex_user_message(malformed) == malformed


def test_markdown_table_values_are_escaped() -> None:
    context = """<environment_context>
      <cwd>/workspace/a|b`c</cwd>
    </environment_context>"""
    result = format_codex_user_message(context)

    assert r"/workspace/a\|b`c" in result


def test_permission_qualifiers_preserve_camel_case() -> None:
    context = """<environment_context>
      <filesystem>
        <permission_profile type="workspaceWrite">
          <file_system type="readOnly" />
        </permission_profile>
      </filesystem>
    </environment_context>"""

    result = format_codex_user_message(context)

    assert "Profile `workspaceWrite`; filesystem `readOnly`." in result

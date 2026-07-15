#!/usr/bin/env python3
"""
Generate a visual style guide showing all message types and rendering styles.

This script creates comprehensive HTML and Markdown files that demonstrate how
different types of Claude transcript messages are rendered, serving both as a
test and as documentation for the visual design.

Session 1: Hand-crafted examples showing various formatting scenarios
Session 2: Auto-generated from dev-docs/messages samples showing all message types
"""

import copy
import json
import sys
import tempfile
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_code_log.converter import convert_jsonl_to_html, convert_jsonl_to
from claude_code_log.html.renderer import generate_projects_index_html
from claude_code_log.markdown.renderer import MarkdownRenderer


def create_style_guide_data():
    """Create comprehensive test data covering all message types and edge cases."""
    return [
        # Basic user message
        {
            "type": "user",
            "timestamp": "2025-06-14T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_001",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": '📝 **User Message Example**\n\nThis is a basic user message with some **markdown formatting**:\n\n- Bullet points work\n- `inline code` is supported\n- [Links](https://example.com) are preserved\n\nCode blocks are also supported:\n```python\ndef hello_world():\n    print("Hello, world!")\n    return 42\n```',
                    }
                ],
            },
        },
        # Basic assistant message
        {
            "type": "assistant",
            "timestamp": "2025-06-14T10:00:30Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_002",
            "requestId": "req_001",
            "message": {
                "id": "style_002",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet-20240229",
                "content": [
                    {
                        "type": "text",
                        "text": "🤖 **Assistant Message Example**\n\nThis is a basic assistant response. It supports the same markdown formatting:\n\n## Headers work too\n\n### Subheaders\n\n> Block quotes look nice\n\n| Tables | Also | Work | Great |\n|--------|------|------|-------|\n| Cell 1 | Cell 2 | Cell 3 | Cell 4 |\n| Data   | More  | Info  | Values |\n\n```javascript\n// Different language syntax highlighting\nfunction greet(name) {\n    console.log(`Hello, ${name}!`);\n    return true;\n}\n```\n\n**Key features:**\n- Full markdown support\n- Syntax highlighting\n- Responsive design\n- Clean typography",
                    }
                ],
            },
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 25,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 120,
                "service_tier": "standard",
            },
        },
        # Tool use message
        {
            "type": "assistant",
            "timestamp": "2025-06-14T10:01:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_003",
            "requestId": "req_002",
            "message": {
                "id": "style_003",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet-20240229",
                "content": [
                    {
                        "type": "text",
                        "text": "🛠️ **Tool Use Example**\n\nLet me create a file to demonstrate tool usage:",
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_style_001",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/style_guide_example.py",
                            "old_string": "",
                            "new_string": '#!/usr/bin/env python3\n"""\nStyle Guide Example File\n\nThis file demonstrates how tool usage appears in transcripts.\n"""\n\nimport os\nimport sys\nfrom typing import List, Dict, Any\n\n\nclass StyleGuideExample:\n    """Example class for the style guide."""\n    \n    def __init__(self, name: str):\n        self.name = name\n        self.items: List[Dict[str, Any]] = []\n    \n    def add_item(self, item: Dict[str, Any]) -> None:\n        """Add an item to the collection."""\n        self.items.append(item)\n        print(f"Added item: {item.get(\'name\', \'Unknown\')}")\n    \n    def process_all(self) -> None:\n        """Process all items in the collection."""\n        for i, item in enumerate(self.items):\n            print(f"Processing item {i+1}: {item}")\n\n\nif __name__ == "__main__":\n    example = StyleGuideExample("Demo")\n    example.add_item({"name": "Sample", "value": 42})\n    example.add_item({"name": "Test", "value": 100})\n    example.process_all()\n    print("Style guide example completed!")',
                        },
                    },
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 15,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 200,
                    "service_tier": "standard",
                },
            },
        },
        # Tool result (success)
        {
            "type": "user",
            "timestamp": "2025-06-14T10:01:01Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_004",
            "toolUseResult": "File created successfully at: /tmp/style_guide_example.py",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_style_001",
                        "content": "File created successfully at: /tmp/style_guide_example.py",
                        "is_error": False,
                    }
                ],
            },
        },
        # Tool use with error result
        {
            "type": "assistant",
            "timestamp": "2025-06-14T10:01:30Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_005",
            "requestId": "req_003",
            "message": {
                "id": "style_005",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet-20240229",
                "content": [
                    {
                        "type": "text",
                        "text": "⚠️ **Tool Error Example**\n\nLet me demonstrate what happens when a tool encounters an error:",
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_style_002",
                        "name": "Bash",
                        "input": {
                            "command": "nonexistent_command --invalid-flag",
                            "description": "This command will fail to demonstrate error handling",
                        },
                    },
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 25,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 45,
                    "service_tier": "standard",
                },
            },
        },
        # Tool result (error)
        {
            "type": "user",
            "timestamp": "2025-06-14T10:01:31Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_006",
            "toolUseResult": "Error: Command 'nonexistent_command' not found in PATH",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_style_002",
                        "content": "bash: line 1: nonexistent_command: command not found\nError: Command 'nonexistent_command' not found in PATH\nExit code: 127",
                        "is_error": True,
                    }
                ],
            },
        },
        # System command message
        {
            "type": "user",
            "timestamp": "2025-06-14T10:02:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_007",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": '⚙️ **System Command Example**\n\n<command-name>style-guide-test</command-name><command-args>--format json --output /tmp/test.json</command-args><command-message>Testing system command rendering in style guide</command-message><command-contents>{"text": "This is the command content with JSON data:\\n{\\n  \\"name\\": \\"Style Guide Test\\",\\n  \\"version\\": \\"1.0.0\\",\\n  \\"description\\": \\"Demonstrates command content formatting\\"\\n}"}</command-contents>',
                    }
                ],
            },
        },
        # Command output
        {
            "type": "user",
            "timestamp": "2025-06-14T10:02:10Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_008",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "📤 **Command Output Example**\n\n<local-command-stdout>Style Guide Test Results:\n✓ Message rendering: PASSED\n✓ Markdown processing: PASSED\n✓ Tool use formatting: PASSED\n✓ Error handling: PASSED\n✓ System commands: PASSED\n✓ Unicode support: PASSED\n\nTotal tests: 6\nPassed: 6\nFailed: 0\n\nTimestamp: 2025-06-14T10:02:10Z\nStatus: ALL_TESTS_PASSED\n\nStyle guide generation completed successfully! 🎉</local-command-stdout>",
                    }
                ],
            },
        },
        # Long text example
        {
            "type": "user",
            "timestamp": "2025-06-14T10:03:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_009",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "📏 **Long Text Example**\n\nThis message demonstrates how long text content is handled by the transcript viewer. The CSS should handle word wrapping automatically, ensuring that even very long messages remain readable and don't break the layout.\n\nLorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.\n\nSed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium, totam rem aperiam, eaque ipsa quae ab illo inventore veritatis et quasi architecto beatae vitae dicta sunt explicabo. Nemo enim ipsam voluptatem quia voluptas sit aspernatur aut odit aut fugit, sed quia consequuntur magni dolores eos qui ratione voluptatem sequi nesciunt.\n\nThe layout should remain clean and readable even with this amount of text content. Line breaks and paragraph spacing should be preserved appropriately.",
                    }
                ],
            },
        },
        # Special characters and emoji
        {
            "type": "assistant",
            "timestamp": "2025-06-14T10:03:30Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "style_guide",
            "version": "1.0.0",
            "uuid": "style_010",
            "requestId": "req_004",
            "message": {
                "id": "style_010",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet-20240229",
                "content": [
                    {
                        "type": "text",
                        "text": '🌍 **Special Characters & Unicode Example**\n\nThe transcript viewer supports full Unicode:\n\n**Accented characters:** café, naïve, résumé, piñata\n**Non-Latin scripts:** 中文, العربية, русский, ελληνικά, हिन्दी\n**Mathematical symbols:** ∑∆√π∞≈≠±×÷\n**Currency symbols:** $¢£€¥₹₿\n**Arrows and symbols:** →←↑↓⇒⇐↔️⚡⭐\n**Emojis:** 🎉🚀🎯🔥💡🌟⚙️📝🎨🎭\n\n**Code with special chars:**\n```python\n# Unicode in code comments: π ≈ 3.14159\ndef calculate_π():\n    return 22 / 7  # Approximation of π\n    \n# String with Unicode\ngreeting = "Hello, 世界! 🌍"\nprint(f"Mathematical: √16 = {16**0.5}")\n```\n\nAll characters should render correctly without breaking the layout!',
                    }
                ],
            },
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 45,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 150,
                "service_tier": "standard",
            },
        },
        # Summary message
        {
            "type": "summary",
            "summary": "📋 **Summary Example** - This style guide demonstrated all major message types: user messages, assistant responses, tool usage (both successful and error cases), system commands, command outputs, long text handling, special character support, and this summary. The transcript viewer properly renders markdown, handles tool interactions, manages text wrapping, and supports full Unicode character sets. All visual elements maintain consistent styling and readability.",
            "leafUuid": "style_010",
        },
    ]


def create_index_style_guide_data():
    """Create sample project data for index template testing."""
    return [
        {
            "name": "claude-code-assistant",
            "path": Path("/fake/path/claude-code-assistant"),
            "html_file": "claude-code-assistant/combined_transcripts.html",
            "jsonl_count": 5,
            "message_count": 127,
            "last_modified": 1700000000.0,
        },
        {
            "name": "-user-workspace-my-web-app",
            "path": Path("/fake/path/-user-workspace-my-web-app"),
            "html_file": "-user-workspace-my-web-app/combined_transcripts.html",
            "jsonl_count": 8,
            "message_count": 203,
            "last_modified": 1700050000.0,
        },
        {
            "name": "data-analysis-project",
            "path": Path("/fake/path/data-analysis-project"),
            "html_file": "data-analysis-project/combined_transcripts.html",
            "jsonl_count": 3,
            "message_count": 89,
            "last_modified": 1700025000.0,
        },
        {
            "name": "-org-internal-tools-automation",
            "path": Path("/fake/path/-org-internal-tools-automation"),
            "html_file": "-org-internal-tools-automation/combined_transcripts.html",
            "jsonl_count": 12,
            "message_count": 445,
            "last_modified": 1700075000.0,
        },
    ]


def load_jsonl_sample(file_path: Path) -> dict | None:
    """Load a single message from a JSONL file."""
    try:
        with open(file_path, encoding="utf-8") as f:
            line = f.readline().strip()
            if line:
                return json.loads(line)
    except (OSError, json.JSONDecodeError) as e:
        print(f"    ⚠️  Warning: Could not load {file_path}: {e}")
    return None


def create_sample_messages_session() -> list[dict]:
    """Create a session from all message samples in dev-docs/messages.

    This function:
    1. Loads all .jsonl samples from dev-docs/messages/
    2. Transforms them in-memory with consistent sessionId, uuid, parentUuid
    3. Orders them logically (user→assistant→tools, sidechain under Task)
    4. Duplicates tools with isSidechain=True (except Task itself)

    Returns:
        List of transformed message dictionaries for a single session
    """
    script_dir = Path(__file__).parent
    samples_dir = script_dir.parent / "dev-docs" / "messages"

    session_id = "sample_messages_session"
    base_time = datetime(2025, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    time_counter = [0]  # Use list for mutable closure

    def new_uuid() -> str:
        return str(uuid_module.uuid4())

    def new_timestamp() -> str:
        time_counter[0] += 1
        dt = base_time + timedelta(seconds=time_counter[0] * 10)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def transform_message(
        msg: dict,
        *,
        uuid: str,
        parent_uuid: str | None,
        is_sidechain: bool = False,
        tool_id_map: dict[str, str] | None = None,
    ) -> dict:
        """Transform a message with new identifiers."""
        result = copy.deepcopy(msg)
        result["sessionId"] = session_id
        result["uuid"] = uuid
        result["parentUuid"] = parent_uuid
        result["timestamp"] = new_timestamp()
        result["isSidechain"] = is_sidechain

        # Update tool_use ids
        if tool_id_map is not None:
            content = result.get("message", {}).get("content", [])
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "tool_use" and "id" in item:
                        old_id = item["id"]
                        new_id = f"toolu_{new_uuid()[:24]}"
                        item["id"] = new_id
                        tool_id_map[old_id] = new_id
                    elif item.get("type") == "tool_result" and "tool_use_id" in item:
                        old_id = item["tool_use_id"]
                        if old_id in tool_id_map:
                            item["tool_use_id"] = tool_id_map[old_id]

        return result

    # Collect samples by category
    user_samples: dict[str, dict] = {}
    assistant_samples: dict[str, dict] = {}
    system_samples: dict[str, dict] = {}
    tool_samples: dict[str, dict] = {}  # key: "ToolName-tool_use" etc.

    # Expected sample names for validation
    expected_user = {
        "user",
        "user_slash_command",
        "user_command",
        "command_output",
        "image",
    }
    expected_assistant = {"assistant", "thinking"}
    expected_system = {"system_info", "summary"}
    # Tools are discovered dynamically

    # Load user samples
    user_dir = samples_dir / "user"
    if user_dir.exists():
        for jsonl_file in user_dir.glob("*.jsonl"):
            name = jsonl_file.stem
            msg = load_jsonl_sample(jsonl_file)
            if msg:
                user_samples[name] = msg
            if name not in expected_user and name not in {"user_sidechain"}:
                print(f"    ⚠️  Warning: Unknown user sample type: {name}")

    # Load assistant samples
    assistant_dir = samples_dir / "assistant"
    if assistant_dir.exists():
        for jsonl_file in assistant_dir.glob("*.jsonl"):
            name = jsonl_file.stem
            msg = load_jsonl_sample(jsonl_file)
            if msg:
                assistant_samples[name] = msg
            if name not in expected_assistant and name not in {"assistant_sidechain"}:
                print(f"    ⚠️  Warning: Unknown assistant sample type: {name}")

    # Load system samples
    system_dir = samples_dir / "system"
    if system_dir.exists():
        for jsonl_file in system_dir.glob("*.jsonl"):
            name = jsonl_file.stem
            msg = load_jsonl_sample(jsonl_file)
            if msg:
                system_samples[name] = msg
            if name not in expected_system and name not in {
                "queue_operation",
                "file_history_snapshot",
            }:
                print(f"    ⚠️  Warning: Unknown system sample type: {name}")

    # Load tool samples
    tools_dir = samples_dir / "tools"
    if tools_dir.exists():
        for jsonl_file in tools_dir.glob("*.jsonl"):
            name = jsonl_file.stem
            msg = load_jsonl_sample(jsonl_file)
            if msg:
                tool_samples[name] = msg

    # Build the message list
    messages: list[dict] = []
    last_uuid: str | None = None
    tool_id_map: dict[str, str] = {}

    # Helper to add a message
    def add_message(
        msg: dict, *, is_sidechain: bool = False, update_last: bool = True
    ) -> str:
        nonlocal last_uuid
        uuid = new_uuid()
        transformed = transform_message(
            msg,
            uuid=uuid,
            parent_uuid=last_uuid,
            is_sidechain=is_sidechain,
            tool_id_map=tool_id_map,
        )
        messages.append(transformed)
        if update_last:
            last_uuid = uuid
        return uuid

    # === MAIN CHAIN MESSAGES ===

    # 1. User messages
    print("    Loading user samples...")
    if "user" in user_samples:
        add_message(user_samples["user"])
    if "user_slash_command" in user_samples:
        add_message(user_samples["user_slash_command"])
    if "user_command" in user_samples:
        add_message(user_samples["user_command"])
    if "command_output" in user_samples:
        add_message(user_samples["command_output"])

    # 2. Assistant message
    print("    Loading assistant samples...")
    if "assistant" in assistant_samples:
        add_message(assistant_samples["assistant"])

    # 3. System info
    print("    Loading system samples...")
    if "system_info" in system_samples:
        add_message(system_samples["system_info"])

    # 4. Image message (user)
    if "image" in user_samples:
        add_message(user_samples["image"])

    # 5. Main chain tools (except Task which comes later)
    print("    Loading tool samples...")

    # Get unique tool names (excluding Task, exit_plan_mode variants, and error results)
    tool_names = set()
    for name in tool_samples:
        if name.endswith("-tool_use"):
            tool_name = name[:-9]  # Remove "-tool_use"
            # Skip Task (handled separately) and exit_plan_mode (duplicate of ExitPlanMode)
            if tool_name not in {"Task", "exit_plan_mode"}:
                tool_names.add(tool_name)

    # Sort tools alphabetically
    sorted_tools = sorted(tool_names)

    # Add each tool (use + result pair)
    for tool_name in sorted_tools:
        use_key = f"{tool_name}-tool_use"
        result_key = f"{tool_name}-tool_result"
        error_key = f"{tool_name}-tool_result_error"

        if use_key in tool_samples:
            # Add tool_use
            add_message(tool_samples[use_key])

            # Add tool_result (prefer success, fallback to error)
            if result_key in tool_samples:
                add_message(tool_samples[result_key])
            elif error_key in tool_samples:
                add_message(tool_samples[error_key])

    # === TASK TOOL WITH SIDECHAIN ===
    print("    Loading Task tool with sidechain...")

    task_use_key = "Task-tool_use"
    task_result_key = "Task-tool_result"

    if task_use_key in tool_samples:
        # Add Task tool_use
        task_uuid = add_message(tool_samples[task_use_key])

        # === SIDECHAIN MESSAGES (appear under Task) ===

        # Save main chain position
        main_chain_last_uuid = last_uuid

        # Start sidechain from the Task message
        last_uuid = task_uuid

        # Add sidechain user
        if "user_sidechain" in user_samples:
            add_message(user_samples["user_sidechain"], is_sidechain=True)
        elif "user" in user_samples:
            # Duplicate regular user as sidechain
            add_message(user_samples["user"], is_sidechain=True)

        # Add sidechain assistant
        if "assistant_sidechain" in assistant_samples:
            add_message(assistant_samples["assistant_sidechain"], is_sidechain=True)
        elif "assistant" in assistant_samples:
            add_message(assistant_samples["assistant"], is_sidechain=True)

        # Add thinking (always sidechain in practice)
        if "thinking" in assistant_samples:
            add_message(assistant_samples["thinking"], is_sidechain=True)

        # Add sidechain tools (all except Task - no nested Tasks)
        for tool_name in sorted_tools:
            use_key = f"{tool_name}-tool_use"
            result_key = f"{tool_name}-tool_result"
            error_key = f"{tool_name}-tool_result_error"

            if use_key in tool_samples:
                # Add tool_use as sidechain
                add_message(tool_samples[use_key], is_sidechain=True)

                # Add tool_result as sidechain
                if result_key in tool_samples:
                    add_message(tool_samples[result_key], is_sidechain=True)
                elif error_key in tool_samples:
                    add_message(tool_samples[error_key], is_sidechain=True)

        # Restore main chain position
        last_uuid = main_chain_last_uuid

        # Add Task tool_result (back on main chain)
        if task_result_key in tool_samples:
            add_message(tool_samples[task_result_key])

    # === SUMMARY (at the end) ===
    if "summary" in system_samples:
        summary = copy.deepcopy(system_samples["summary"])
        summary["leafUuid"] = last_uuid
        messages.append(summary)

    print(f"    ✓ Loaded {len(messages)} messages from samples")
    return messages


def generate_style_guide():
    """Generate the complete style guide HTML files."""
    script_dir = Path(__file__).parent
    output_dir = script_dir / "style_guide_output"
    output_dir.mkdir(exist_ok=True)

    print("🎨 Generating Claude Code Log Style Guide...")

    # Generate transcript style guide with two sessions:
    # Session 1: Hand-crafted examples
    # Session 2: Auto-generated from dev-docs/messages samples
    print("  📝 Creating transcript style guide...")

    print("    Session 1: Hand-crafted examples...")
    style_guide_data = create_style_guide_data()

    print("    Session 2: Message samples from dev-docs/messages...")
    sample_messages_data = create_sample_messages_session()

    # Combine both sessions
    all_data = style_guide_data + sample_messages_data

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        jsonl_file = temp_path / "style_guide.jsonl"

        # Write combined style guide data
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for entry in all_data:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Convert to HTML
        transcript_html = convert_jsonl_to_html(
            jsonl_file, output_dir / "transcript_style_guide.html"
        )
        print(f"    ✅ Transcript style guide (HTML) saved to: {transcript_html}")

        # Convert to Markdown
        transcript_md = convert_jsonl_to(
            "md",
            jsonl_file,
            output_dir / "transcript_style_guide.md",
        )
        print(f"    ✅ Transcript style guide (Markdown) saved to: {transcript_md}")

    # Generate index style guide
    print("  📂 Creating index style guide...")
    index_data = create_index_style_guide_data()

    # HTML index
    index_html_content = generate_projects_index_html(
        index_data, from_date="last week", to_date="today"
    )
    index_html_file = output_dir / "index_style_guide.html"
    index_html_file.write_text(index_html_content, encoding="utf-8")
    print(f"    ✅ Index style guide (HTML) saved to: {index_html_file}")

    # Markdown index
    md_renderer = MarkdownRenderer()
    index_md_content = md_renderer.generate_projects_index(
        index_data, from_date="last week", to_date="today"
    )
    index_md_file = output_dir / "index_style_guide.md"
    index_md_file.write_text(index_md_content, encoding="utf-8")
    print(f"    ✅ Index style guide (Markdown) saved to: {index_md_file}")

    # Create a main index file
    print("  🏠 Creating main style guide index...")
    main_index_content = """<!DOCTYPE html>
<html lang='en'>
<head>
    <meta charset='UTF-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1.0'>
    <title>Claude Code Log Style Guide</title>
    <style>
        body {
            font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', monospace;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
            background-color: #fafafa;
            color: #333;
        }
        h1 {
            text-align: center;
            color: #2c3e50;
            margin-bottom: 30px;
            font-size: 2.5em;
        }
        .guide-links {
            display: grid;
            gap: 20px;
            margin: 40px 0;
        }
        .guide-card {
            background: white;
            border-radius: 8px;
            padding: 25px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            border-left: 4px solid #2196f3;
        }
        .guide-card:hover {
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            transform: translateY(-1px);
            transition: all 0.2s ease;
        }
        .guide-title {
            font-size: 1.4em;
            font-weight: 600;
            margin-bottom: 10px;
            color: #2196f3;
        }
        .guide-description {
            color: #666;
            margin-bottom: 15px;
        }
        .guide-link {
            display: inline-block;
            padding: 8px 16px;
            background-color: #2196f3;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            font-weight: 500;
        }
        .guide-link:hover {
            background-color: #1976d2;
        }
        .info {
            background-color: #e3f2fd;
            border: 1px solid #2196f3;
            border-radius: 4px;
            padding: 15px;
            margin: 20px 0;
        }
        .info strong {
            color: #1565c0;
        }
    </style>
</head>
<body>
    <h1>🎨 Claude Code Log Style Guide</h1>
    
    <div class="info">
        <strong>About this style guide:</strong> This collection demonstrates how different types of Claude transcript messages are rendered in the HTML viewer. It serves as both visual documentation and a testing reference for developers working on the transcript rendering system.
    </div>
    
    <div class="guide-links">
        <div class="guide-card">
            <div class="guide-title">📝 Transcript Viewer Style Guide</div>
            <div class="guide-description">
                Comprehensive demonstration of all message types including user messages, assistant responses, tool usage, system commands, errors, and special formatting. Shows markdown rendering, code highlighting, tool interactions, and Unicode support.
            </div>
            <a href="transcript_style_guide.html" class="guide-link">View Transcript Guide</a>
        </div>
        
        <div class="guide-card">
            <div class="guide-title">📂 Project Index Style Guide</div>
            <div class="guide-description">
                Shows how the project index page renders with multiple projects, summary statistics, and various project naming conventions. Demonstrates the dashboard view that users see when browsing multiple transcript collections.
            </div>
            <a href="index_style_guide.html" class="guide-link">View Index Guide</a>
        </div>
    </div>
    
    <div class="info">
        <strong>Usage:</strong> These style guides are automatically generated from representative test data. They demonstrate the current visual design and can be used to verify that template changes render correctly across all message types.
    </div>
</body>
</html>"""

    main_index_file = output_dir / "index.html"
    main_index_file.write_text(main_index_content, encoding="utf-8")

    print(f"    ✅ Main index saved to: {main_index_file}")
    print()
    print("🎉 Style guide generation complete!")
    print(f"📂 Open {main_index_file} in your browser to view the style guides")
    print()
    print("Style guide files generated:")
    print(f"  • Main index: {main_index_file}")
    print("  HTML:")
    print(f"    • Transcript guide: {output_dir / 'transcript_style_guide.html'}")
    print(f"    • Index guide: {output_dir / 'index_style_guide.html'}")
    print("  Markdown:")
    print(f"    • Transcript guide: {output_dir / 'transcript_style_guide.md'}")
    print(f"    • Index guide: {output_dir / 'index_style_guide.md'}")


if __name__ == "__main__":
    # Ensure stdout uses UTF-8 for emoji output when running as a script
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    generate_style_guide()

#!/usr/bin/env python3
"""
Generate a visual style guide showing all message types and rendering styles.

This script creates a comprehensive HTML file that demonstrates how different
types of Claude transcript messages are rendered, serving both as a test
and as documentation for the visual design.
"""

import json
import sys
import tempfile
from pathlib import Path

from claude_code_log.converter import (
    convert_jsonl_to_html,
    generate_projects_index_html,
)


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


def generate_style_guide():
    """Generate the complete style guide HTML files."""
    script_dir = Path(__file__).parent
    output_dir = script_dir / "style_guide_output"
    output_dir.mkdir(exist_ok=True)

    print("🎨 Generating Claude Code Log Style Guide...")

    # Generate transcript style guide
    print("  📝 Creating transcript style guide...")
    style_guide_data = create_style_guide_data()

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        jsonl_file = temp_path / "style_guide.jsonl"

        # Write style guide data
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for entry in style_guide_data:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Convert to HTML
        transcript_html = convert_jsonl_to_html(
            jsonl_file, output_dir / "transcript_style_guide.html"
        )

        print(f"    ✅ Transcript style guide saved to: {transcript_html}")

    # Generate index style guide
    print("  📂 Creating index style guide...")
    index_data = create_index_style_guide_data()
    index_html_content = generate_projects_index_html(
        index_data, from_date="last week", to_date="today"
    )

    index_file = output_dir / "index_style_guide.html"
    index_file.write_text(index_html_content, encoding="utf-8")

    print(f"    ✅ Index style guide saved to: {index_file}")

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
    print(f"  • Transcript guide: {output_dir / 'transcript_style_guide.html'}")
    print(f"  • Index guide: {output_dir / 'index_style_guide.html'}")


if __name__ == "__main__":
    # Ensure stdout uses UTF-8 for emoji output when running as a script
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    generate_style_guide()

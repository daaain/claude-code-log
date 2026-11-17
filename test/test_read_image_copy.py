"""A Read of an image keeps one copy of it, not two.

Claude Code records a Read of an image twice: as an ``image`` block in the
tool_result, which renders, and as ``toolUseResult.file.base64``, which
nothing reads. The second copy is dropped when the entry is parsed, so it
stays out of memory, render workers and the cache.
"""

import base64
import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.models import UserTranscriptEntry

_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAA"
    "BJRU5ErkJggg=="
)
_SID = "aaaaaaaa-0000-0000-0000-000000000001"


def _read_file_info() -> dict[str, Any]:
    return {
        "base64": _PNG,
        "type": "image/png",
        "originalSize": len(base64.b64decode(_PNG)),
        "dimensions": {"originalWidth": 1, "originalHeight": 1},
    }


def _read_result_entry() -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2025-07-01T10:00:01Z",
        "parentUuid": "a1",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": "2.1.0",
        "uuid": "u2",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _PNG,
                            },
                        }
                    ],
                }
            ],
        },
        "toolUseResult": {"type": "image", "file": _read_file_info()},
    }


def _read_call_entry() -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": "2025-07-01T10:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": "2.1.0",
        "uuid": "a1",
        "requestId": "req_1",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/shot.png"},
                }
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


class TestTheCopyIsDroppedOnParse:
    def test_base64_goes_and_the_rest_of_the_file_stays(self):
        entry = UserTranscriptEntry.model_validate(_read_result_entry())
        assert isinstance(entry.toolUseResult, dict)
        file_info = entry.toolUseResult["file"]
        assert "base64" not in file_info
        expected = {k: v for k, v in _read_file_info().items() if k != "base64"}
        assert file_info == expected
        assert entry.toolUseResult["type"] == "image"

    def test_the_rendered_copy_is_untouched(self):
        entry = UserTranscriptEntry.model_validate(_read_result_entry())
        assert _PNG in entry.model_dump_json()

    def test_the_input_dict_is_not_mutated(self):
        raw = _read_result_entry()
        UserTranscriptEntry.model_validate(raw)
        assert raw["toolUseResult"]["file"]["base64"] == _PNG

    @pytest.mark.parametrize(
        "tool_use_result",
        [
            "Error: file not found",
            [{"type": "text", "text": "x"}],
            {"stdout": "ok", "stderr": ""},
            {"file": {"filePath": "/tmp/a.py", "content": "print(1)\n"}},
            {"file": "not a dict"},
        ],
    )
    def test_other_results_pass_through(self, tool_use_result: Any):
        raw = _read_result_entry()
        raw["toolUseResult"] = tool_use_result
        entry = UserTranscriptEntry.model_validate(raw)
        assert entry.toolUseResult == tool_use_result


def test_the_cache_holds_the_image_once_and_the_page_still_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from claude_code_log.converter import convert_jsonl_to_html

    db_path = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    project = tmp_path / "-tmp-project"
    project.mkdir()
    (project / f"{_SID}.jsonl").write_text(
        "".join(
            json.dumps(e) + "\n" for e in (_read_call_entry(), _read_result_entry())
        ),
        encoding="utf-8",
    )

    page = Path(convert_jsonl_to_html(project, use_cache=True, silent=True))

    with sqlite3.connect(db_path) as conn:
        blobs = [row[0] for row in conn.execute("SELECT content FROM messages")]
    assert blobs, "the conversion should have cached its entries"
    stored = "".join(zlib.decompress(blob).decode("utf-8") for blob in blobs)
    assert stored.count(_PNG) == 1
    assert '"base64":' not in stored  # the key, not the source's type

    html = page.read_text(encoding="utf-8")
    assert 'class="tool-result-image"' in html

#!/usr/bin/env python3
"""Generate the synthesized ``workflow_scriptpath`` test fixture.

Companion to ``gen_workflow_fixture.py`` (the inline-``script`` shape);
this fixture covers the OTHER Workflow invocation shapes observed in real
sessions (workflow-shape-variety follow-up to #174):

    test/test_data/workflow_scriptpath/
      <trunk>.jsonl                              trunk with TWO scriptPath tool_uses
      <trunk>/
        subagents/workflows/wf_sp01/
          journal.jsonl                          normal run: 1 agent
          agent-agsp0001.jsonl
          agent-agsp0001.meta.json
        workflows/
          wf_sp01.json                           snapshot WITH the script that ran
          wf_fail01.json                         snapshot-ONLY failed run (no run dir)

Shapes exercised:

- ``scriptPath`` (+ ``args``) invocation — the tool_use input carries NO
  source; the renderer must recover the script from the snapshot's
  ``script`` field.
- A meta description containing a backslash-escaped quote (``team\\'s``) —
  regression coverage for the JS-string parse.
- ``wf_fail01``: a run that died before launching any agent (script error)
  — a snapshot with ``status: failed`` + ``error`` but no run dir/journal.

Re-run to regenerate: ``python3 scripts/gen_workflow_scriptpath_fixture.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "test" / "test_data" / "workflow_scriptpath"

TRUNK_SID = "22220000-0000-4000-8000-000000000002"
TS = "2026-06-28T09:00:00.000Z"
TS_FAIL = "2026-06-28T09:05:00.000Z"

SP_SCRIPT = (
    "export const meta = {\n"
    "  name: 'docs-sweep',\n"
    "  description: 'Sweep the team\\'s docs tree in batches',\n"
    "  phases: [\n"
    "    { title: 'Sweep', detail: 'one worker per batch' },\n"
    "  ],\n"
    "}\n"
    "phase('Sweep')\n"
    "return await agent('Sweep docs: ' + JSON.stringify(args))\n"
)
SP_ARGS = {"target": "docs/", "batches": ["a", "b"]}
SP_PATH = "/home/u/sweeps/docs-sweep.workflow.js"

FAIL_SCRIPT = (
    "export const meta = {\n"
    "  name: 'docs-sweep-broken',\n"
    "  description: 'Sweep variant that dies before launching agents',\n"
    "  phases: [\n"
    "    { title: 'Sweep', detail: 'never reached' },\n"
    "  ],\n"
    "}\n"
    "const batches = args.batches\n"
    "await parallel(batches.map(b => () => agent('sweep ' + b)))\n"
)
FAIL_PATH = "/home/u/sweeps/docs-sweep-broken.workflow.js"


def _jsonl(path: Path, rows: "list[dict[str, Any]]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _trunk() -> "list[dict[str, Any]]":
    common = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": TRUNK_SID,
        "version": "2.1.2",
    }
    return [
        {
            "type": "user",
            "uuid": "spu00001",
            "parentUuid": None,
            "timestamp": TS,
            **common,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Run the docs sweep workflow from its file.",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "spa00001",
            "parentUuid": "spu00001",
            "timestamp": TS,
            **common,
            "message": {
                "id": "msg_spa00001",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "Launching the saved sweep script."},
                    {
                        "type": "tool_use",
                        "id": "toolu_wfsp01",
                        "name": "Workflow",
                        # scriptPath shape: NO inline script in the input
                        "input": {"scriptPath": SP_PATH, "args": SP_ARGS},
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "uuid": "spu00002",
            "parentUuid": "spa00001",
            "timestamp": TS,
            **common,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_wfsp01",
                        "content": (
                            "Workflow launched in background. Task ID: task_sp01\n"
                            "Summary: Sweep the team's docs tree in batches.\n"
                            f"Transcript dir: {TRUNK_SID}/subagents/workflows/wf_sp01\n"
                            f"Script file: {SP_PATH}"
                        ),
                    }
                ],
            },
            "toolUseResult": {
                "isAsync": True,
                "status": "async_launched",
                "runId": "wf_sp01",
                "taskId": "task_sp01",
                "transcriptDir": f"{TRUNK_SID}/subagents/workflows/wf_sp01",
                "scriptPath": SP_PATH,
            },
        },
        {
            "type": "assistant",
            "uuid": "spa00002",
            "parentUuid": "spu00002",
            "timestamp": TS_FAIL,
            **common,
            "message": {
                "id": "msg_spa00002",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "Launching the broken variant."},
                    {
                        "type": "tool_use",
                        "id": "toolu_wffail01",
                        "name": "Workflow",
                        "input": {"scriptPath": FAIL_PATH},
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "uuid": "spu00003",
            "parentUuid": "spa00002",
            "timestamp": TS_FAIL,
            **common,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_wffail01",
                        "content": (
                            "Workflow launched in background. Task ID: task_fail01\n"
                            "Summary: Sweep variant that dies before launching agents.\n"
                            f"Transcript dir: {TRUNK_SID}/subagents/workflows/wf_fail01\n"
                            f"Script file: {FAIL_PATH}"
                        ),
                    }
                ],
            },
            "toolUseResult": {
                "isAsync": True,
                "status": "async_launched",
                "runId": "wf_fail01",
                "taskId": "task_fail01",
                "transcriptDir": f"{TRUNK_SID}/subagents/workflows/wf_fail01",
                "scriptPath": FAIL_PATH,
            },
        },
    ]


def _agent_rows() -> "list[dict[str, Any]]":
    agent_sid = f"{TRUNK_SID}#agent-agsp0001"
    common = {
        "isSidechain": True,
        "userType": "external",
        "cwd": "/repo",
        "sessionId": agent_sid,
        "version": "2.1.2",
        "timestamp": TS,
        "agentId": "agsp0001",
    }
    return [
        {
            "type": "user",
            "uuid": "agsp0001_u1",
            "parentUuid": None,
            **common,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Sweep docs batches a and b."}],
            },
        },
        {
            "type": "assistant",
            "uuid": "agsp0001_a1",
            "parentUuid": "agsp0001_u1",
            **common,
            "message": {
                "id": "msg_agsp0001_a1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": "Batch a and b swept; 2 stale pages flagged.",
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        },
    ]


def main() -> None:
    trunk_dir = FIXTURE / TRUNK_SID
    run_dir = trunk_dir / "subagents" / "workflows" / "wf_sp01"
    wf_dir = trunk_dir / "workflows"

    _jsonl(FIXTURE / f"{TRUNK_SID}.jsonl", _trunk())

    _jsonl(
        run_dir / "journal.jsonl",
        [
            {"type": "started", "key": "v2:hsp0", "agentId": "agsp0001"},
            {
                "type": "result",
                "key": "v2:hsp0",
                "agentId": "agsp0001",
                "result": "Batch a and b swept; 2 stale pages flagged.",
            },
        ],
    )
    _jsonl(run_dir / "agent-agsp0001.jsonl", _agent_rows())
    (run_dir / "agent-agsp0001.meta.json").write_text(
        json.dumps({"agentType": "workflow-subagent"}, indent=2), encoding="utf-8"
    )

    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "wf_sp01.json").write_text(
        json.dumps(
            {
                "runId": "wf_sp01",
                "taskId": "task_sp01",
                "status": "completed",
                "workflowName": "docs-sweep",
                "timestamp": TS,
                "durationMs": 2000,
                "agentCount": 1,
                "totalTokens": 101,
                "totalToolCalls": 2,
                "defaultModel": "claude-sonnet-4-6",
                "script": SP_SCRIPT,
                "scriptPath": SP_PATH,
                "args": SP_ARGS,
                "summary": "Sweep the team's docs tree in batches",
                "phases": [{"title": "Sweep", "detail": "one worker per batch"}],
                "workflowProgress": [
                    {"type": "workflow_phase", "index": 1, "title": "Sweep"},
                    {
                        "type": "workflow_agent",
                        "index": 0,
                        "label": "sweep:a",
                        "phaseIndex": 1,
                        "phaseTitle": "Sweep",
                        "agentId": "agsp0001",
                        "model": "claude-sonnet-4-6",
                        "state": "done",
                        "attempt": 1,
                        "tokens": 101,
                        "toolCalls": 2,
                        "durationMs": 900,
                        "resultPreview": "Batch a and b swept",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Snapshot-ONLY failed run: deliberately no subagents/workflows/wf_fail01 dir.
    (wf_dir / "wf_fail01.json").write_text(
        json.dumps(
            {
                "runId": "wf_fail01",
                "taskId": "task_fail01",
                "status": "failed",
                "workflowName": "docs-sweep-broken",
                "timestamp": TS_FAIL,
                "durationMs": 12,
                "agentCount": 0,
                "totalTokens": 0,
                "totalToolCalls": 0,
                "defaultModel": "claude-sonnet-4-6",
                "script": FAIL_SCRIPT,
                "scriptPath": FAIL_PATH,
                "summary": "Sweep variant that dies before launching agents",
                "error": (
                    "Error: undefined is not an object (evaluating 'batches.map')\n"
                    "    at <anonymous> (workflow.js:9:10)"
                ),
                "phases": [{"title": "Sweep", "detail": "never reached"}],
                "workflowProgress": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"workflow_scriptpath fixture written under {FIXTURE}")


if __name__ == "__main__":
    main()

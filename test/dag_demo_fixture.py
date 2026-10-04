"""A synthetic project shaped like the DAG mockups (minimal theme, P6).

``write_dag_demo(directory, turns=1)`` writes one session (plus its
``subagents/``) modelled on ``work/minimal-theme-dag-mockups/DagRail.dc.html``:
per repetition, a turn that starts two background agents whose steps
interleave with the main line's own tool calls and whose
``<task-notification>`` results arrive later, a synchronous agent with a
nested agent of its own, and a second prompt that is rewound — the earliest
branch continues the main line, the later one becomes a fork lane.

Used by ``test/test_minimal_dag_browser.py`` and, scaled up with ``turns``,
for the engine's performance measurement (work/minimal-theme-dag.md, P6
"As built"). Not a test module itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SESSION = "dade0000-0000-4000-8000-000000000001"
BASE = datetime(2026, 10, 3, 14, 2, 11, tzinfo=timezone.utc)


def _iso(seconds: float) -> str:
    stamp = BASE + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


class _Writer:
    def __init__(self, turn: int) -> None:
        self.turn = turn
        self.offset = turn * 1800  # each repetition 30 minutes later
        self.count = 0

    def uid(self, tag: str) -> str:
        self.count += 1
        return f"{tag}{self.turn:03d}-{self.count:04d}-4000-8000-000000000000"

    def entry(
        self,
        kind: str,
        parent: Optional[str],
        at: float,
        content: Any,
        *,
        agent: Optional[str] = None,
        uuid: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": kind,
            "uuid": uuid or self.uid("a" if kind == "assistant" else "u"),
            "parentUuid": parent,
            "isSidechain": agent is not None,
            "userType": "external",
            "cwd": "/home/synthetic/theme",
            "sessionId": SESSION,
            "version": "2.1.180",
            "timestamp": _iso(self.offset + at),
        }
        if agent is not None:
            entry["agentId"] = agent
        if kind == "user":
            entry["message"] = {"role": "user", "content": content}
        else:
            entry["message"] = {
                "id": "msg_" + entry["uuid"],
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-7" if agent is None else "claude-haiku-4-5",
                "stop_reason": None,
                "content": content,
                "usage": {"input_tokens": 1200, "output_tokens": 80},
            }
        entry.update(extra)
        return entry


def _text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def _tool(tool_id: str, name: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]


def _result(tool_id: str, content: Any, is_error: bool = False) -> list[dict[str, Any]]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return [block]


def _call(
    w: _Writer,
    parent: str,
    at: float,
    name: str,
    inp: dict[str, Any],
    output: str,
    *,
    agent: Optional[str] = None,
    is_error: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """A tool call and its result one second later; returns (entries, last uuid)."""
    tool_id = "toolu_" + w.uid("t")[:20].replace("-", "")
    use = w.entry("assistant", parent, at, _tool(tool_id, name, inp), agent=agent)
    res = w.entry(
        "user",
        use["uuid"],
        at + 1,
        _result(tool_id, output, is_error),
        agent=agent,
    )
    return [use, res], res["uuid"]


def _agent_file(
    session_dir: Path,
    agent_id: str,
    tool_use_id: str,
    description: str,
    entries: list[dict[str, Any]],
) -> None:
    sub = session_dir / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    (sub / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "description": description,
                "toolUseId": tool_use_id,
            }
        ),
        encoding="utf-8",
    )


def _agent_steps(
    w: _Writer,
    agent: str,
    prompt: str,
    start: float,
    steps: list[tuple[float, str, dict[str, Any], str, bool]],
    final: tuple[float, str],
) -> list[dict[str, Any]]:
    entries = [w.entry("user", None, start, prompt, agent=agent)]
    parent = entries[0]["uuid"]
    for at, name, inp, output, is_error in steps:
        pair, parent = _call(
            w, parent, at, name, inp, output, agent=agent, is_error=is_error
        )
        entries.extend(pair)
    entries.append(w.entry("assistant", parent, final[0], _text(final[1]), agent=agent))
    return entries


def _notification(agent: str, description: str, result: str, ms: int) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{agent}</task-id>\n<status>completed</status>\n"
        f'<summary>Agent "{description}" completed</summary>\n'
        f"<result>{result}</result>\n"
        f"<usage>total_tokens: 48400\ntool_uses: 4\nduration_ms: {ms}</usage>\n"
        "</task-notification>"
    )


def _launch(tool_id: str, agent: str, description: str) -> tuple[Any, dict[str, Any]]:
    text = (
        "Async agent launched successfully.\n"
        f"agentId: {agent} (internal ID - do not mention to user. Use to "
        "resume later if needed.)\nThe agent is working in the background. "
        "You will be notified automatically when it completes."
    )
    extra = {
        "toolUseResult": {
            "isAsync": True,
            "status": "async_launched",
            "agentId": agent,
            "description": description,
            "prompt": description,
            "outputFile": f"/tmp/tasks/{agent}.output",
        }
    }
    return _result(tool_id, [{"type": "text", "text": text}]), extra


def _sync_result(tool_id: str, text: str, agent: str) -> list[dict[str, Any]]:
    return _result(
        tool_id,
        [
            {"type": "text", "text": text},
            {
                "type": "text",
                "text": f"agentId: {agent} (use SendMessage with to: '{agent}' "
                "to continue this agent)\n<usage>total_tokens: 12100\n"
                "tool_uses: 2\nduration_ms: 62000</usage>",
            },
        ],
    )


def _repetition(
    w: _Writer, session_dir: Path, parent: Optional[str], wide: int = 0
) -> tuple[list[dict[str, Any]], str]:
    """One mockup-shaped stretch; returns (main entries, last main uuid).

    ``wide`` adds that many more background agents to the first turn (each
    one step, its notification arriving between the two others'), so the
    turn has ``3 + wide`` branches — the "+N more branches" overflow.
    """
    n = w.turn
    main: list[dict[str, Any]] = []
    a_id, b_id, c_id, d_id = (
        f"a{n:03d}audit",
        f"b{n:03d}pyg",
        f"c{n:03d}sync",
        f"d{n:03d}leaf",
    )

    u1 = w.entry(
        "user",
        parent,
        0,
        "Can we add a --theme option so the HTML output can use a more compact "
        "stylesheet? Light and dark please, and the default should stay exactly "
        "as it is.",
    )
    a1 = w.entry(
        "assistant",
        u1["uuid"],
        5,
        _text(
            "Two things can run in parallel: auditing the hard-coded colours and "
            "finding a dark syntax palette. I'll start both in the background and "
            "look at how the stylesheets load meanwhile."
        ),
    )
    main += [u1, a1]

    # Two background agents.
    tool_a = f"toolu_{n:03d}spawnA"
    tool_b = f"toolu_{n:03d}spawnB"
    spawn_a = w.entry(
        "assistant",
        a1["uuid"],
        7,
        _tool(
            tool_a,
            "Task",
            {
                "description": "Audit hard-coded colours",
                "prompt": "Find every hard-coded colour in the stylesheets.",
                "subagent_type": "Explore",
                "run_in_background": True,
            },
        ),
    )
    content, extra = _launch(tool_a, a_id, "Audit hard-coded colours")
    launch_a = w.entry("user", spawn_a["uuid"], 7.5, content, **extra)
    spawn_b = w.entry(
        "assistant",
        launch_a["uuid"],
        8,
        _tool(
            tool_b,
            "Task",
            {
                "description": "Find dark Pygments styles",
                "prompt": "Which Pygments styles suit a dark page?",
                "subagent_type": "general-purpose",
                "run_in_background": True,
            },
        ),
    )
    content, extra = _launch(tool_b, b_id, "Find dark Pygments styles")
    launch_b = w.entry("user", spawn_b["uuid"], 8.5, content, **extra)
    main += [spawn_a, launch_a, spawn_b, launch_b]
    extras: list[tuple[str, str]] = []
    for k in range(wide):
        e_id = f"e{n:03d}x{k:02d}"
        tool_e = f"toolu_{n:03d}spawnE{k:02d}"
        description = f"Survey corner {k + 1}"
        spawn_e = w.entry(
            "assistant",
            launch_b["uuid"] if not extras else extras[-1][1],
            9 + k * 0.5,
            _tool(
                tool_e,
                "Task",
                {
                    "description": description,
                    "prompt": f"Look into corner {k + 1} of the stylesheets.",
                    "subagent_type": "Explore",
                    "run_in_background": True,
                },
            ),
        )
        content, extra = _launch(tool_e, e_id, description)
        launch_e = w.entry("user", spawn_e["uuid"], 9.2 + k * 0.5, content, **extra)
        main += [spawn_e, launch_e]
        extras.append((e_id, launch_e["uuid"]))
        _agent_file(
            session_dir,
            e_id,
            tool_e,
            description,
            _agent_steps(
                w,
                e_id,
                f"Look into corner {k + 1} of the stylesheets.",
                10 + k,
                [
                    (
                        12 + k,
                        "Grep",
                        {"pattern": f"corner-{k + 1}", "path": "components/"},
                        f"{k + 2} matches",
                        False,
                    )
                ],
                (20 + k, f"Corner {k + 1} is clean."),
            ),
        )
    if extras:
        launch_b = {"uuid": extras[-1][1]}

    pair, last = _call(
        w,
        launch_b["uuid"],
        13,
        "Grep",
        {"pattern": "global_styles.css", "path": "claude_code_log/html"},
        "templates/transcript.html:14\ntemplates/index.html:12\n"
        "templates/archive_search.html:9",
    )
    main += pair
    pair, last = _call(
        w,
        last,
        30,
        "Read",
        {"file_path": "claude_code_log/cli.py", "offset": 210, "limit": 5},
        '210  @click.option(\n211      "--open-browser",\n212      is_flag=True,\n'
        '213      help="Open the generated HTML in a browser.",\n214  )',
    )
    main += pair

    _agent_file(
        session_dir,
        a_id,
        tool_a,
        "Audit hard-coded colours",
        _agent_steps(
            w,
            a_id,
            "Find every hard-coded colour in the stylesheets.",
            9,
            [
                (
                    10,
                    "Grep",
                    {"pattern": "#[0-9a-fA-F]{3,8}", "path": "components/"},
                    "412 matches in 14 files",
                    False,
                ),
                (
                    29,
                    "Read",
                    {"file_path": "components/timeline.html", "offset": 84, "limit": 7},
                    "84  const typeColors = {\n85      user: { background: '#fff3e0' },\n86  };",
                    False,
                ),
                (
                    51,
                    "Bash",
                    {"command": 'rg -c "var\\(--" components/'},
                    "rg: components/: No such file or directory (os error 2)",
                    True,
                ),
                (
                    90,
                    "Bash",
                    {
                        "command": 'rg -c "var\\(--" claude_code_log/html/templates/components/'
                    },
                    "96 matches in 9 files",
                    False,
                ),
            ],
            (
                139,
                "14 files and 412 literals; 11 variables are referenced but never declared.",
            ),
        ),
    )
    _agent_file(
        session_dir,
        b_id,
        tool_b,
        "Find dark Pygments styles",
        _agent_steps(
            w,
            b_id,
            "Which Pygments styles suit a dark page?",
            9,
            [
                (
                    15,
                    "Bash",
                    {"command": "uv run python -m pygments -L styles"},
                    "Styles:\n* default\n* github-dark",
                    False,
                ),
                (
                    44,
                    "WebFetch",
                    {
                        "url": "https://pygments.org/styles",
                        "prompt": "List dark styles",
                    },
                    "github-dark, one-dark, monokai",
                    False,
                ),
            ],
            (
                69,
                "github-dark and one-dark both ship with Pygments; github-dark sits closest to the palette.",
            ),
        ),
    )

    note_b = w.entry(
        "user",
        last,
        70,
        _notification(
            b_id,
            "Find dark Pygments styles",
            "Use github-dark for the dark theme, scoped under the dark selectors.",
            62000,
        ),
    )
    main.append(note_b)
    pair, last = _call(
        w,
        note_b["uuid"],
        79,
        "Edit",
        {
            "file_path": "claude_code_log/cli.py",
            "old_string": "@click.command()",
            "new_string": '@click.command()\n@click.option("--theme")',
        },
        "The file has been updated.",
    )
    main += pair
    for k, (e_id, _uuid) in enumerate(extras):
        note_e = w.entry(
            "user",
            last,
            100 + k,
            _notification(
                e_id, f"Survey corner {k + 1}", f"Corner {k + 1} is clean.", 11000
            ),
        )
        main.append(note_e)
        last = note_e["uuid"]
    note_a = w.entry(
        "user",
        last,
        140,
        _notification(
            a_id,
            "Audit hard-coded colours",
            "14 files, 412 literals. timeline.html sets item colours inline.",
            133000,
        ),
    )
    main.append(note_a)
    answer = w.entry(
        "assistant",
        note_a["uuid"],
        221,
        _text(
            "The compact theme is wired in as an extra stylesheet layered after "
            "global_styles.css, and code blocks use github-dark in dark mode."
        ),
    )
    main.append(answer)

    # Turn 2 (rewound): the earliest prompt continues main, with a
    # synchronous agent that spawns a nested one.
    u2 = w.entry(
        "user",
        answer["uuid"],
        411,
        "Does the timeline pick up the dark palette as well?",
    )
    tool_c = f"toolu_{n:03d}spawnC"
    spawn_c = w.entry(
        "assistant",
        u2["uuid"],
        415,
        _tool(
            tool_c,
            "Task",
            {
                "description": "Check vis-timeline colours",
                "prompt": "Where does vis-timeline get its colours?",
                "subagent_type": "Explore",
            },
        ),
    )
    result_c = w.entry(
        "user",
        spawn_c["uuid"],
        480,
        _sync_result(tool_c, "Colours are inline in JavaScript.", c_id),
    )
    tail = w.entry(
        "assistant",
        result_c["uuid"],
        492,
        _text(
            "Not yet: vis-timeline gets its colours inline from JavaScript. "
            "I'll read them from the same custom properties."
        ),
    )
    main += [u2, spawn_c, result_c, tail]

    tool_d = f"toolu_{n:03d}spawnD"
    c_entries = [
        w.entry(
            "user", None, 416, "Where does vis-timeline get its colours?", agent=c_id
        )
    ]
    pair, c_last = _call(
        w,
        c_entries[0]["uuid"],
        420,
        "Grep",
        {"pattern": "background-color", "path": "components/timeline.html"},
        "3 matches",
        agent=c_id,
    )
    c_entries += pair
    nested_use = w.entry(
        "assistant",
        c_last,
        430,
        _tool(
            tool_d,
            "Task",
            {
                "description": "Read vis-timeline docs",
                "prompt": "How are group colours set?",
                "subagent_type": "general-purpose",
            },
        ),
        agent=c_id,
    )
    nested_res = w.entry(
        "user",
        nested_use["uuid"],
        460,
        _sync_result(tool_d, "Groups take a className.", d_id),
        agent=c_id,
    )
    c_entries += [nested_use, nested_res]
    c_entries.append(
        w.entry(
            "assistant",
            nested_res["uuid"],
            470,
            _text("Colours are inline in JavaScript."),
            agent=c_id,
        )
    )
    _agent_file(session_dir, c_id, tool_c, "Check vis-timeline colours", c_entries)
    _agent_file(
        session_dir,
        d_id,
        tool_d,
        "Read vis-timeline docs",
        _agent_steps(
            w,
            d_id,
            "How are group colours set?",
            431,
            [
                (
                    440,
                    "WebFetch",
                    {
                        "url": "https://visjs.github.io/vis-timeline/docs",
                        "prompt": "group colours",
                    },
                    "className per group",
                    False,
                )
            ],
            (455, "Groups take a className; colours come from CSS."),
        ),
    )

    # The fork: a later prompt rewound from the same answer.
    f1 = w.entry(
        "user",
        answer["uuid"],
        1074,
        "Actually, skip the flag. Pick light or dark from the OS, with a toggle in the page.",
    )
    f2 = w.entry(
        "assistant",
        f1["uuid"],
        1109,
        _text(
            "Then no CLI change is needed: both palettes ship in every page and a "
            "data-theme attribute overrides the system choice."
        ),
    )
    pair, f_last = _call(
        w,
        f2["uuid"],
        1241,
        "Edit",
        {
            "file_path": "claude_code_log/cli.py",
            "old_string": '@click.option("--theme")',
            "new_string": "",
        },
        "The file has been updated.",
    )
    main += [f1, f2] + pair
    return main, tail["uuid"]


def write_dag_demo(directory: Path, turns: int = 1, wide: int = 0) -> Path:
    """Write the demo project into ``directory``; returns ``directory``.

    ``turns`` repeats the stretch (each 30 minutes after the previous), so a
    large page can be built for timing: every repetition adds ~40 cards and
    five lanes. ``wide`` adds that many background agents to each first
    turn (P7's branch overflow).
    """
    directory.mkdir(parents=True, exist_ok=True)
    session_dir = directory / SESSION
    entries: list[dict[str, Any]] = []
    parent: Optional[str] = None
    for n in range(turns):
        main, parent = _repetition(_Writer(n), session_dir, parent, wide)
        entries += main
    (directory / f"{SESSION}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return directory


TEAM_SESSION = "dade0000-0000-4000-8000-0000000000ee"


def write_team_demo(directory: Path) -> Path:
    """A lead and one teammate exchanging messages both ways (P7 anchors).

    The lead spawns ``alice`` (a named teammate: ``team_name`` + ``name``),
    alice reports back with ``SendMessage`` — delivered to the lead as a
    ``<teammate-message>`` — and the lead answers with a ``SendMessage`` of
    its own, which reaches alice's thread the same way. A third message has
    no counterpart anywhere (it must get no link). Returns ``directory``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    session_dir = directory / TEAM_SESSION
    w = _Writer(0)
    agent = "f00dfacecafe0001"
    tool_spawn = "toolu_team_spawn_alice"
    team = {"teamName": "styles"}

    def lead(kind: str, parent: Optional[str], at: float, content: Any, **extra: Any):
        entry = w.entry(kind, parent, at, content, **team, **extra)
        entry["sessionId"] = TEAM_SESSION
        return entry

    def mate(kind: str, parent: Optional[str], at: float, content: Any):
        entry = w.entry(kind, parent, at, content, agent=agent)
        entry["sessionId"] = TEAM_SESSION
        return entry

    report = "Relay coverage is now 96%: ten tests for deliver_to_remote."
    reply = "Thanks. Please cover calculate_next_retry as well."
    u1 = lead("user", None, 0, "Start a styles team and send alice to the relay tests.")
    spawn = lead(
        "assistant",
        u1["uuid"],
        5,
        _tool(
            tool_spawn,
            "Task",
            {
                "description": "Run alice's test work",
                "subagent_type": "general-purpose",
                "name": "alice",
                "team_name": "styles",
                "prompt": "You are alice. Add relay tests and report back.",
            },
        ),
    )
    result = lead(
        "user",
        spawn["uuid"],
        6,
        _result(
            tool_spawn,
            [
                {"type": "text", "text": "Spawned alice."},
                {
                    "type": "text",
                    "text": f"agentId: {agent} (use SendMessage with to: 'alice')",
                },
            ],
        ),
        toolUseResult={"status": "completed", "agentId": agent},
    )
    note = lead(
        "user",
        result["uuid"],
        60,
        f'<teammate-message teammate_id="alice" color="blue" summary="relay done">\n{report}\n</teammate-message>',
    )
    tool_reply = "toolu_team_reply"
    send = lead(
        "assistant",
        note["uuid"],
        70,
        _tool(
            tool_reply,
            "SendMessage",
            {"type": "message", "recipient": "alice", "content": reply},
        ),
    )
    sent = lead(
        "user",
        send["uuid"],
        71,
        _result(tool_reply, '{"success": true, "message": "Message sent to alice"}'),
    )
    stray = lead(
        "user",
        sent["uuid"],
        90,
        '<teammate-message teammate_id="alice" color="blue">\nalice heartbeat: still here.\n</teammate-message>',
    )
    done = lead(
        "assistant", stray["uuid"], 95, _text("alice is on the retry tests now.")
    )
    (directory / f"{TEAM_SESSION}.jsonl").write_text(
        "\n".join(
            json.dumps(e) for e in [u1, spawn, result, note, send, sent, stray, done]
        )
        + "\n",
        encoding="utf-8",
    )

    m1 = mate("user", None, 6, "You are alice. Add relay tests and report back.")
    m2 = mate("assistant", m1["uuid"], 20, _text("Writing the relay tests."))
    tool_report = "toolu_team_report"
    m3 = mate(
        "assistant",
        m2["uuid"],
        59,
        _tool(
            tool_report,
            "SendMessage",
            {"type": "message", "recipient": "team-lead", "content": report},
        ),
    )
    m4 = mate(
        "user",
        m3["uuid"],
        59.5,
        _result(
            tool_report, '{"success": true, "message": "Message sent to team-lead"}'
        ),
    )
    m5 = mate(
        "user",
        m4["uuid"],
        72,
        f'<teammate-message teammate_id="team-lead" color="cyan">\n{reply}\n</teammate-message>',
    )
    m6 = mate("assistant", m5["uuid"], 80, _text("Adding calculate_next_retry tests."))
    sub = session_dir / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [m1, m2, m3, m4, m5, m6]) + "\n",
        encoding="utf-8",
    )
    (sub / f"agent-{agent}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Run alice's test work",
                "toolUseId": tool_spawn,
            }
        ),
        encoding="utf-8",
    )
    return directory


WORKFLOW_SESSION = "dade0000-0000-4000-8000-0000000000f1"
WORKFLOW_RUN = "wf_theme01"
WORKFLOW_TASK = "task_wftheme01"

# (agent id, label, phase, model, start, [(at, tool, input, output)], end,
#  answer or None, state, transcript?)
_WF_AGENTS: list[tuple[Any, ...]] = [
    (
        "wa01loader",
        "map:loader",
        "Map",
        "claude-haiku-4-5",
        9,
        [
            (
                12,
                "Grep",
                {"pattern": "load_transcript", "path": "claude_code_log"},
                "7 matches",
            )
        ],
        31,
        {"area": "loader", "summary": "Workflow runs load per session directory."},
        "done",
        True,
    ),
    (
        "wa02render",
        "map:renderer",
        "Map",
        "claude-haiku-4-5",
        9.5,
        [
            (
                14,
                "Read",
                {"file_path": "claude_code_log/renderer.py"},
                "3087  def _splice_workflow_runs",
            ),
            (27, "Grep", {"pattern": "in_workflow_sidechannel"}, "4 matches"),
        ],
        44,
        {
            "area": "renderer",
            "summary": "The splice runs last and re-registers every node.",
        },
        "done",
        True,
    ),
    (
        "wa03lanes",
        "map:lanes",
        "Map",
        "claude-haiku-4-5",
        10,
        [
            (
                18,
                "Read",
                {"file_path": "claude_code_log/lanes.py"},
                "annotate_lanes(roots)",
            )
        ],
        36,
        {"area": "lanes", "summary": "Lanes come from the render tree alone."},
        "done",
        True,
    ),
    (
        "wa04engine",
        "map:engine",
        "Map",
        "claude-haiku-4-5",
        10.5,
        [(21, "Bash", {"command": "rg -c relayout minimal_dag.js"}, "31")],
        25,
        None,  # failed: no result
        "failed",
        True,
    ),
    (
        "wa05styles",
        "map:styles",
        "Map",
        "claude-haiku-4-5",
        11,
        [
            (
                16,
                "Grep",
                {"pattern": "workflow", "path": "components/minimal"},
                "12 matches",
            )
        ],
        40,
        {"area": "styles", "summary": "Workflow groups indent like sub-agents."},
        "done",
        True,
    ),
    (
        "wa06docs",
        "map:docs",
        "Map",
        "claude-haiku-4-5",
        11.5,
        [],
        20,
        {"area": "docs", "summary": "workflows.md covers the splice."},
        "done",
        False,  # no transcript on disk: a plain row
    ),
    (
        "wa07synth",
        "synthesize",
        "Synthesize",
        "claude-opus-4-7",
        50,
        [(58, "Read", {"file_path": "dev-docs/workflows.md"}, "## 5. The splice")],
        75,
        "## Plan\n\nMake each workflow agent a lane, spawned at its phase.",
        "done",
        True,
    ),
]


def write_workflow_demo(directory: Path) -> Path:
    """A Workflow run fanning out to many parallel agents (workflow lanes).

    One turn: a prompt, a ``Workflow`` call whose run has two phases —
    *Map* with six agents (five with a side-channel transcript, one of which
    failed without a result, and one with no transcript at all: a plain
    row) and *Synthesize* with one — then, in the same turn, a synchronous
    ``Task`` agent and an answer. Every agent's steps carry real times, so
    the Map agents interleave with each other. Returns ``directory``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    session_dir = directory / WORKFLOW_SESSION
    run_dir = session_dir / "subagents" / "workflows" / WORKFLOW_RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    w = _Writer(0)

    def main(kind: str, parent: Optional[str], at: float, content: Any, **extra: Any):
        entry = w.entry(kind, parent, at, content, **extra)
        entry["sessionId"] = WORKFLOW_SESSION
        return entry

    script = (
        "export const meta = {\n"
        "  name: 'theme-map',\n"
        "  description: 'Map the theme code, one agent per area',\n"
        "  phases: [{ title: 'Map' }, { title: 'Synthesize' }],\n"
        "}\n"
        "phase('Map')\n"
        "const areas = await parallel(AREAS.map(a => () => agent(a.prompt)))\n"
        "phase('Synthesize')\n"
        "return await agent('Merge: ' + JSON.stringify(areas))\n"
    )
    u1 = main(
        "user",
        None,
        0,
        "Map how workflows are rendered: one agent per area, then a synthesis.",
    )
    a1 = main(
        "assistant",
        u1["uuid"],
        4,
        _text("I'll fan a mapping workflow out over the six areas."),
    )
    use = main(
        "assistant",
        a1["uuid"],
        6,
        _tool("toolu_wfdemo", "Workflow", {"script": script}),
    )
    res = main(
        "user",
        use["uuid"],
        7,
        _result(
            "toolu_wfdemo",
            f"Workflow launched in background. Task ID: {WORKFLOW_TASK}\n"
            "Summary: Map the theme code, one agent per area.",
        ),
        toolUseResult={
            "isAsync": True,
            "status": "async_launched",
            "runId": WORKFLOW_RUN,
            "taskId": WORKFLOW_TASK,
        },
    )
    entries = [u1, a1, use, res]
    tool_c = "toolu_wfdemo_task"
    spawn = main(
        "assistant",
        res["uuid"],
        80,
        _tool(
            tool_c,
            "Task",
            {
                "description": "Check the lane styles",
                "prompt": "Do workflow rows need styles of their own?",
                "subagent_type": "Explore",
            },
        ),
    )
    result = main(
        "user",
        spawn["uuid"],
        96,
        _sync_result(tool_c, "No: they reuse the row styles.", "c900check"),
    )
    answer = main(
        "assistant",
        result["uuid"],
        100,
        _text("Each workflow agent becomes a lane spawned at its phase."),
    )
    entries += [spawn, result, answer]
    (directory / f"{WORKFLOW_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    task_entries = _agent_steps(
        w,
        "c900check",
        "Do workflow rows need styles of their own?",
        81,
        [
            (
                85,
                "Grep",
                {"pattern": "workflow_agent", "path": "components/minimal"},
                "3 matches",
                False,
            )
        ],
        (94, "No: they reuse the row styles."),
    )
    for entry in task_entries:
        entry["sessionId"] = WORKFLOW_SESSION
    _agent_file(session_dir, "c900check", tool_c, "Check the lane styles", task_entries)

    journal: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for index, (
        agent_id,
        label,
        phase,
        model,
        start,
        steps,
        end,
        answer_value,
        state,
        transcript,
    ) in enumerate(_WF_AGENTS):
        journal.append(
            {"type": "started", "key": f"v2:{agent_id}", "agentId": agent_id}
        )
        if answer_value is not None:
            results.append(
                {
                    "type": "result",
                    "key": f"v2:{agent_id}",
                    "agentId": agent_id,
                    "result": answer_value,
                }
            )
        phase_index = 1 if phase == "Map" else 2
        progress.append(
            {
                "type": "workflow_agent",
                "index": index,
                "label": label,
                "phaseIndex": phase_index,
                "phaseTitle": phase,
                "agentId": agent_id,
                "model": model,
                "state": state,
                "attempt": 1,
                "tokens": 1500 + 200 * index,
                "toolCalls": len(steps) + 1,
                "durationMs": int((end - start) * 1000),
                "resultPreview": ""
                if answer_value is None
                else json.dumps(answer_value)[:60],
            }
        )
        if not transcript:
            continue
        sid = f"{WORKFLOW_SESSION}#agent-{agent_id}"
        rows = [
            w.entry("user", None, start, f"You map the {label} area.", agent=agent_id)
        ]
        parent = rows[0]["uuid"]
        for at, name, inp, output in steps:
            pair, parent = _call(w, parent, at, name, inp, output, agent=agent_id)
            rows.extend(pair)
        if answer_value is not None:
            rows.append(
                w.entry(
                    "assistant",
                    parent,
                    end,
                    _text(
                        answer_value
                        if isinstance(answer_value, str)
                        else json.dumps(answer_value)
                    ),
                    agent=agent_id,
                )
            )
        for row in rows:
            row["sessionId"] = sid
            if row["type"] == "assistant":
                row["message"]["model"] = model
        (run_dir / f"agent-{agent_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        (run_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "workflow-subagent"}), encoding="utf-8"
        )
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(r) for r in journal + results) + "\n", encoding="utf-8"
    )
    snapshot = {
        "runId": WORKFLOW_RUN,
        "taskId": WORKFLOW_TASK,
        "status": "completed",
        "workflowName": "theme-map",
        "timestamp": _iso(6),
        "durationMs": 70000,
        "agentCount": 6,
        "totalTokens": sum(p["tokens"] for p in progress),
        "totalToolCalls": sum(p["toolCalls"] for p in progress),
        "defaultModel": "claude-haiku-4-5",
        "script": script,
        "phases": [
            {"title": "Map", "detail": "one agent per area"},
            {"title": "Synthesize", "detail": "merge the areas"},
        ],
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "Map"},
            {"type": "workflow_phase", "index": 2, "title": "Synthesize"},
        ]
        + progress,
        "result": {"plan": "Workflow agents as lanes."},
    }
    snap_dir = session_dir / "workflows"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{WORKFLOW_RUN}.json").write_text(
        json.dumps(snapshot, indent=1), encoding="utf-8"
    )
    return directory

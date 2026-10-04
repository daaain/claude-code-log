"""A session that grows on disk the way a live one does (minimal theme, P7b).

``LiveDagScript(project)`` writes a project whose session is still being
recorded, then grows it stage by stage — appending lines to the trunk JSONL
and to the agents' ``subagents/agent-<id>.jsonl`` files exactly as Claude
Code does while it runs:

``start``
    A prompt; a background (async) agent *A* is launched; a synchronous
    agent *C* is spawned and is still running — its sidechain file holds a
    prompt and one tool call, the trunk has no tool_result for it yet.
``c_grows``, ``c_grows_more``
    *C*'s transcript grows (one more tool call each). Nothing is appended to
    the trunk — the parent blocks on the synchronous call.
``c_merges``
    *C* answers; the trunk gets its tool_result (the merge row) and the
    main line carries on.
``a_merges``
    *A* answers; its ``<task-notification>`` arrives on the main line (the
    async merge row), then a second prompt and its answer.
``rewind``
    The second prompt is rewound: a new prompt with the same parent. The
    earliest branch keeps continuing main; the new one becomes a fork lane.
``fork_grows``
    The fork gets another answer (a pure append at the end of the page).

Timestamps are ``base`` + seconds; ``base`` defaults to the demo's fixed
time, and the live browser tests pass "a minute ago" so that the page reads
the session as live (a running lane is shown as running only while the
page's newest activity is recent — dev-docs/minimal-theme.md § 8).

Used by ``test/test_minimal_dag_live_browser.py`` (end to end, through a real
watch + server) and ``test/test_lanes.py`` (the lane data at each stage,
across render paths). Not a test module itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from test.dag_demo_fixture import (
    BASE,
    _launch,
    _notification,
    _result,
    _sync_result,
    _text,
    _tool,
    _Writer,
)

LIVE_SESSION = "dade0000-0000-4000-8000-00000000000b"
AGENT_A = "a0live0async"  # background agent: open lane, then a notification
AGENT_C = "c0live0sync"  # synchronous agent: running, then its result
LANE_A = f"agent-{AGENT_A}"
LANE_C = f"agent-{AGENT_C}"
TOOL_A = "toolu_live_spawnA"
TOOL_C = "toolu_live_spawnC"

STAGES = (
    "start",
    "c_grows",
    "c_grows_more",
    "c_merges",
    "a_merges",
    "rewind",
    "fork_grows",
)


class LiveDagScript:
    """Writes the project at ``start`` and appends each later stage."""

    def __init__(
        self,
        project: Path,
        base: Optional[datetime] = None,
        session: str = LIVE_SESSION,
        suffix: str = "",
        uid_space: int = 0,
    ) -> None:
        """``session`` and ``suffix`` (appended to the agent and tool ids)
        let two scripts share one project — two sessions on a combined page;
        give the second a different ``uid_space`` so entry uuids don't
        collide."""
        self.project = project
        self.base = base or BASE
        self.session = session
        self.agent_a = AGENT_A + suffix
        self.agent_c = AGENT_C + suffix
        self.tool_a = TOOL_A + suffix
        self.tool_c = TOOL_C + suffix
        self.trunk = project / f"{self.session}.jsonl"
        self.subagents = project / self.session / "subagents"
        self.w = _Writer(uid_space)
        self.last: dict[str, str] = {}
        self.stage = ""

    # ---- writing -----------------------------------------------------
    def _entry(
        self, kind: str, parent: Optional[str], at: float, content: Any, **kw: Any
    ) -> dict[str, Any]:
        entry = self.w.entry(kind, parent, at, content, **kw)
        entry["sessionId"] = self.session
        entry["timestamp"] = self.time(at)
        return entry

    def time(self, seconds: float) -> str:
        """The ISO timestamp written for ``seconds`` into the session."""
        stamp = self.base + timedelta(seconds=seconds)
        return (
            stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
        )

    def _append(self, path: Path, entries: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def _meta(self, agent: str, tool_use_id: str, description: str) -> None:
        self.subagents.mkdir(parents=True, exist_ok=True)
        (self.subagents / f"agent-{agent}.meta.json").write_text(
            json.dumps(
                {
                    "agentType": "Explore",
                    "description": description,
                    "toolUseId": tool_use_id,
                }
            ),
            encoding="utf-8",
        )

    def _agent(self, agent: str, entries: list[dict[str, Any]]) -> None:
        self._append(self.subagents / f"agent-{agent}.jsonl", entries)
        self.last[agent] = entries[-1]["uuid"]

    def _calls(
        self, agent: str, steps: list[tuple[float, str, dict[str, Any], str]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        parent = self.last[agent]
        for at, name, inp, output in steps:
            tool_id = "toolu_" + self.w.uid("t")[:20].replace("-", "")
            use = self._entry(
                "assistant", parent, at, _tool(tool_id, name, inp), agent=agent
            )
            res = self._entry(
                "user", use["uuid"], at + 1, _result(tool_id, output), agent=agent
            )
            out += [use, res]
            parent = res["uuid"]
        self.last[agent] = parent
        return out

    # ---- stages ------------------------------------------------------
    def start(self) -> None:
        self.project.mkdir(parents=True, exist_ok=True)
        u1 = self._entry(
            "user",
            None,
            0,
            "Audit the stylesheets and check the timeline colours, please.",
        )
        a1 = self._entry(
            "assistant",
            u1["uuid"],
            2,
            _text("I'll audit in the background and check the timeline myself."),
        )
        spawn_a = self._entry(
            "assistant",
            a1["uuid"],
            3,
            _tool(
                self.tool_a,
                "Task",
                {
                    "description": "Audit hard-coded colours",
                    "prompt": "Find every hard-coded colour.",
                    "subagent_type": "Explore",
                    "run_in_background": True,
                },
            ),
        )
        content, extra = _launch(self.tool_a, self.agent_a, "Audit hard-coded colours")
        launch_a = self._entry("user", spawn_a["uuid"], 3.5, content, **extra)
        spawn_c = self._entry(
            "assistant",
            launch_a["uuid"],
            4,
            _tool(
                self.tool_c,
                "Task",
                {
                    "description": "Check vis-timeline colours",
                    "prompt": "Where does vis-timeline get its colours?",
                    "subagent_type": "Explore",
                },
            ),
        )
        self._append(self.trunk, [u1, a1, spawn_a, launch_a, spawn_c])
        self.last["main"] = spawn_c["uuid"]

        self._meta(self.agent_a, self.tool_a, "Audit hard-coded colours")
        self._agent(
            self.agent_a,
            [
                self._entry(
                    "user", None, 4, "Find every hard-coded colour.", agent=self.agent_a
                )
            ],
        )
        self._agent(
            self.agent_a,
            self._calls(
                self.agent_a,
                [(5, "Grep", {"pattern": "#[0-9a-f]{6}"}, "412 matches in 14 files")],
            ),
        )
        self._meta(self.agent_c, self.tool_c, "Check vis-timeline colours")
        self._agent(
            self.agent_c,
            [
                self._entry(
                    "user",
                    None,
                    5,
                    "Where does vis-timeline get its colours?",
                    agent=self.agent_c,
                )
            ],
        )
        self._agent(
            self.agent_c,
            self._calls(
                self.agent_c,
                [(6, "Read", {"file_path": "components/timeline.html"}, "84 lines")],
            ),
        )

    def c_grows(self) -> None:
        self._agent(
            self.agent_c,
            self._calls(
                self.agent_c,
                [(9, "Grep", {"pattern": "background-color"}, "3 matches")],
            ),
        )

    def c_grows_more(self) -> None:
        self._agent(
            self.agent_c,
            self._calls(
                self.agent_c, [(12, "Bash", {"command": "rg -c className"}, "7")]
            ),
        )

    def c_merges(self) -> None:
        self._agent(
            self.agent_c,
            [
                self._entry(
                    "assistant",
                    self.last[self.agent_c],
                    15,
                    _text("Colours are inline in JavaScript."),
                    agent=self.agent_c,
                )
            ],
        )
        result = self._entry(
            "user",
            self.last["main"],
            16,
            _sync_result(
                self.tool_c, "Colours are inline in JavaScript.", self.agent_c
            ),
            toolUseResult={"status": "completed", "agentId": self.agent_c},
        )
        after = self._entry(
            "assistant",
            result["uuid"],
            17,
            _text("The timeline sets its colours inline; I'll move them to CSS."),
        )
        self._append(self.trunk, [result, after])
        self.last["main"] = after["uuid"]

    def a_merges(self) -> None:
        self._agent(
            self.agent_a,
            self._calls(self.agent_a, [(20, "Read", {"file_path": "global.css"}, "ok")])
            + [
                self._entry(
                    "assistant",
                    self.last[self.agent_a],
                    24,
                    _text("14 files, 412 literals."),
                    agent=self.agent_a,
                )
            ],
        )
        note = self._entry(
            "user",
            self.last["main"],
            26,
            _notification(
                self.agent_a,
                "Audit hard-coded colours",
                "14 files, 412 literals.",
                22000,
            ),
        )
        reply = self._entry(
            "assistant", note["uuid"], 28, _text("The audit found 412 literals.")
        )
        u2 = self._entry(
            "user", reply["uuid"], 40, "Use custom properties for all of them."
        )
        a2 = self._entry(
            "assistant", u2["uuid"], 42, _text("Replacing the literals now.")
        )
        self._append(self.trunk, [note, reply, u2, a2])
        self.last["main"] = a2["uuid"]
        self.last["fork_parent"] = reply["uuid"]

    def rewind(self) -> None:
        u2b = self._entry(
            "user",
            self.last["fork_parent"],
            60,
            "Actually, only the timeline colours for now.",
        )
        a2b = self._entry(
            "assistant", u2b["uuid"], 62, _text("Just the timeline, then.")
        )
        self._append(self.trunk, [u2b, a2b])
        self.last["fork"] = a2b["uuid"]

    def fork_grows(self) -> None:
        more = self._entry(
            "assistant",
            self.last["fork"],
            70,
            _text("The timeline now reads its colours from custom properties."),
        )
        self._append(self.trunk, [more])
        self.last["fork"] = more["uuid"]

    # ---- driving -----------------------------------------------------
    def advance(self, stage: str) -> None:
        """Apply ``stage`` (stages must be applied in ``STAGES`` order)."""
        expected = STAGES[STAGES.index(self.stage) + 1] if self.stage else STAGES[0]
        assert stage == expected, f"stage {stage!r} applied out of order"
        step: Callable[[], None] = getattr(self, stage)
        step()
        self.stage = stage

    def run_to(self, stage: str) -> None:
        """Apply every stage up to and including ``stage``."""
        while self.stage != stage:
            nxt = STAGES[STAGES.index(self.stage) + 1] if self.stage else STAGES[0]
            self.advance(nxt)

"""Composition pin: #294 (count image-bearing steering cards) must hold on top
of #303 (place UUID-less queue-ops in chronological order).

Both changes touch the same order-sensitive seam. #294 seeds a suppression
budget from ``queued_command`` cards keyed by ``(session, version, text)``;
#303 decides where a UUID-less ``remove`` lands, which decides the *version*
the suppression pass infers for it (``last_version_by_session``, built in
render order). If the two did not compose, the ``remove`` for an image-bearing
steering delivery would be attributed to the wrong harness version and render
as a duplicate legacy card instead of being suppressed.

Fixtures synthetic; no private data. See test_session_id_ordering.py (dir-mode
loading) and test_steering_queued_command.py (steering suppression).

**Why this asserts on the suppression count and not on the imbalance
warning.** It looks like an oversight and it is not, so do not "fix" it by
adding entries until a warning fires — that makes the fixture non-minimal for
no gain. Version-spanning is *required* for the chronological splice to change
the ``remove``'s inferred version at all; that is what lands the orphan under
``_VB``. But the imbalance warning is gated on ``version_key in qc_versions``
— it fires only for a version that actually had a counted card — and ``_VB``
has none. So the minimal discriminating fixture *cannot* produce a warning.

That gate is deliberate rather than incidental, which is the other half of why
padding the fixture would be the wrong fix: an orphaned ``remove`` under a
version with no counted cards at all is far more likely to be an archive
artifact than a miscount of ours, so warning there would be noise. The warning
is scoped to the case where we have positive evidence the pass should have
paired something.
The suppression count is the cleaner signal anyway: it is the behaviour, where
the warning is only our report of it. The "warnings 2 -> 0" shape needs an
orphan landing under a version that also carries text cards, which the real
archive has and a minimal synthetic would have to manufacture.
"""

import json
from pathlib import Path

from claude_code_log.converter import convert_jsonl_to_html

_SID = "sess-compose-1"
# Both in the text-content ``remove`` era (>= 2.1.205 — see the era boundary in
# dev-docs/messages.md). That matters: this fixture's ``remove`` carries text,
# and a null-content one is filtered out before the suppression pass ever sees
# it, so a version from the legacy era would make the fixture unrepresentable
# rather than merely unusual.
_VA = "2.1.205"  # harness version when the steering was issued
_VB = "2.1.210"  # session resumed after an upgrade
_T = "please rerun the failing test"  # neutral steering text
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _ts(sec: int) -> str:
    return f"2026-07-11T07:00:0{sec}.000Z"


def _user(uuid, parent, text, version, sec):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": version,
        "timestamp": _ts(sec),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(uuid, parent, text, version, sec):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": version,
        "timestamp": _ts(sec),
        "message": {
            "id": "m" + uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _image_qc(uuid, parent, text, version, sec):
    """In-DAG queued_command whose prompt is a *list* (image-bearing)."""
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": version,
        "timestamp": _ts(sec),
        "attachment": {
            "type": "queued_command",
            "commandMode": "prompt",
            "origin": {"kind": "human"},
            "timestamp": _ts(sec),
            "prompt": [
                {"type": "text", "text": text},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _PNG,
                    },
                },
            ],
        },
    }


def _remove(text, sec):
    """UUID-less legacy queue-op — the entry #303 splices chronologically."""
    return {
        "type": "queue-operation",
        "operation": "remove",
        "timestamp": _ts(sec),
        "content": text,
        "sessionId": _SID,
    }


def _write(tmp_path: Path, entries) -> Path:
    d = tmp_path / "-proj-compose"
    d.mkdir()
    (d / f"{_SID}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return d


def _entries():
    # vA block, then the paired remove chronologically (ts=4) BETWEEN the vA
    # block and the vB block. #303 splices the remove to ts=4 (under vA, whose
    # card seeds the budget); appended-at-end it would land under vB and orphan.
    return [
        _user("u1", None, "start", _VA, 1),
        _image_qc("qc1", "u1", _T, _VA, 2),
        _assistant("a1", "qc1", "ok", _VA, 3),
        _remove(_T, 4),
        _user("u2", "a1", "resumed after upgrade", _VB, 5),
        _assistant("a2", "u2", "ok2", _VB, 6),
    ]


def test_image_steering_suppression_composes_with_chronological_splice(tmp_path):
    """With both fixes, the image-bearing steering renders once (its card) and
    the paired remove is suppressed, not duplicated — proving #294's count and
    #303's ordering compose on the shared seam."""
    d = _write(tmp_path, _entries())
    html = Path(
        convert_jsonl_to_html(d, tmp_path / "out", use_cache=False, silent=True)
    ).read_text()

    # The image-bearing card renders (the #294 fix), and its paired remove is
    # suppressed — exactly one steering card, not a duplicate legacy one.
    assert f"base64,{_PNG}" in html, "image-bearing steering card did not render"
    assert html.count("User (steering)") == 1, (
        "expected the remove suppressed (1 steering card); a count of 2 means "
        "the remove was orphaned — the two fixes did not compose"
    )

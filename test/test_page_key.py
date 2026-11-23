"""The per-install secret behind what pages must keep from other local pages
(``claude_code_log/page_key.py``): the recent page's data, and the token a
session page's probe answers to."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import stat
from pathlib import Path
from typing import Iterator

import pytest

from claude_code_log import page_key


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "keys" / "page.key"
    monkeypatch.setenv(page_key.ENV_KEY_FILE, str(path))
    page_key.reset()
    yield path
    page_key.reset()


def test_the_key_is_made_once_and_kept(key_file: Path) -> None:
    assert not key_file.exists()
    first = page_key.install_key()
    assert len(first) == 32
    assert key_file.exists()
    page_key.reset()
    assert page_key.install_key() == first


def test_only_the_owner_can_read_the_key(key_file: Path) -> None:
    page_key.install_key()
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_a_damaged_key_file_is_an_error_not_a_new_key(key_file: Path) -> None:
    """Replacing it silently would strand every page made with the old one."""
    key_file.parent.mkdir(parents=True)
    key_file.write_text("not a key")
    with pytest.raises(ValueError, match=re.escape(str(key_file))):
        page_key.install_key()


def test_processes_starting_together_agree_on_one_key(key_file: Path) -> None:
    """Render workers can all reach for the key at once. The file must never
    be seen half made: one that exists but is still empty read as damaged."""
    script = (
        "import time, sys\n"
        "from claude_code_log import page_key\n"
        "while time.time() < float(sys.argv[1]): pass\n"
        "print(page_key.install_key().hex())\n"
    )
    import time

    start = str(time.time() + 1.5)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, start],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    results = [proc.communicate(timeout=60) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs), [err for _, err in results]
    assert len({out.strip() for out, _ in results}) == 1
    assert bytes.fromhex(results[0][0].strip()) == page_key.install_key()


def test_a_probe_token_is_per_session_and_stable(key_file: Path) -> None:
    a = page_key.probe_token("session-a")
    assert a == page_key.probe_token("session-a")
    assert a != page_key.probe_token("session-b")
    assert len(a) == 32 and int(a, 16) >= 0


def test_the_token_and_the_recent_key_reveal_nothing_of_each_other(
    key_file: Path,
) -> None:
    """Each is derived from the install key under its own label, so a page
    that holds one (recent.html holds the recent key) holds nothing else."""
    recent = page_key.recent_key()
    assert recent != page_key.install_key()
    assert recent.hex()[:32] != page_key.probe_token("recent-data")


def _decrypt(key: bytes, blob: str) -> bytes:
    """What recent.js does with WebCrypto, in Python: HKDF-SHA256 per chunk
    of 8,160 bytes (salt: the nonce, info: the chunk's index), XORed in."""
    import hashlib
    import hmac

    raw = base64.b64decode(blob)
    nonce, ct = raw[:16], raw[16:]
    prk = hmac.digest(nonce, key, hashlib.sha256)
    out = bytearray()
    for index, start in enumerate(range(0, len(ct), 8160)):
        info = index.to_bytes(4, "big")
        stream, block = b"", b""
        for counter in range(1, 256):
            block = hmac.digest(prk, block + info + bytes([counter]), hashlib.sha256)
            stream += block
            if len(stream) >= min(8160, len(ct) - start):
                break
        chunk = ct[start : start + 8160]
        out += bytes(a ^ b for a, b in zip(chunk, stream))
    return bytes(out)


@pytest.mark.parametrize("size", [0, 1, 8159, 8160, 8161, 100_000])
def test_recent_data_round_trips(key_file: Path, size: int) -> None:
    plain = json.dumps({"pad": "Zürich ✓ " * (size // 10 + 1)})[: max(size, 2)].encode()
    blob = page_key.encrypt_recent(plain)
    assert _decrypt(page_key.recent_key(), blob) == plain
    assert page_key.decrypt_recent(blob) == plain


def test_each_write_uses_a_fresh_nonce(key_file: Path) -> None:
    plain = b'{"sessions": ["secret-session"]}'
    a, b = page_key.encrypt_recent(plain), page_key.encrypt_recent(plain)
    assert a != b
    assert b"secret-session" not in base64.b64decode(a)


def test_a_key_that_cannot_be_had_leaves_session_pages_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following from disk is a convenience: a key file that can't be made
    (a read-only home) or read must not fail the conversion. The session
    page is then written without the probe, as a combined page is."""
    from claude_code_log.converter import load_transcript
    from claude_code_log.html.renderer import HtmlRenderer

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the key's folder would go")
    monkeypatch.setenv(page_key.ENV_KEY_FILE, str(blocker / "page.key"))
    page_key.reset()
    try:
        data = Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        entries = load_transcript(data, silent=True)
        session_id = next(e.sessionId for e in entries if getattr(e, "sessionId", None))
        html = HtmlRenderer().generate_session(entries, session_id)
        assert "ccl-probe-token" not in html
        assert 'id="transcript"' in html or "id='transcript'" in html
    finally:
        page_key.reset()

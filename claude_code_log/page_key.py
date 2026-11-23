"""A per-install secret, for what the pages must keep from other local pages.

A page opened from ``file://`` can't fetch another file, but it can run one
as a script, and it can frame another page. Two things the archive writes
would otherwise give themselves away to any HTML file the reader opens
locally (a download, an attachment):

- ``recent-data.js``: data shipped as a script, so that ``recent.html`` can
  reload it from disk. Any page that includes it gets every recent
  session. It is written encrypted (:func:`encrypt_recent`), and only
  ``recent.html`` carries the key (:func:`recent_key`).
- A session page's follow-from-disk probe (live_probe_post.js), which posts
  the transcript to a parent that names the page's token. The token
  (:func:`probe_token`) appears in that page and nowhere else: not in a
  link, not in the JSONL.

Both are derived from one random key, made on first use and kept in a file
only its owner can read (:func:`key_path`; ``CLAUDE_CODE_LOG_KEY_FILE``
overrides where). Derived, so that the pages stay the same from one run to
the next, and so that what one page holds reveals nothing about the rest.
The key file itself is never written into the output, and a local page
can't read it: it is neither script nor page.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

ENV_KEY_FILE = "CLAUDE_CODE_LOG_KEY_FILE"

KEY_BYTES = 32
NONCE_BYTES = 16
# HKDF-SHA256's longest output (255 blocks of 32 bytes, RFC 5869). WebCrypto
# derives it in one call in every engine; Firefox refuses PBKDF2 outputs
# past 256 bytes, which is why the keystream isn't PBKDF2.
CHUNK_BYTES = 8160

_lock = threading.Lock()
_key: Optional[bytes] = None


def key_path() -> Path:
    """Where the install key lives: ``CLAUDE_CODE_LOG_KEY_FILE``, or
    ``~/.claude-code-log/page.key``."""
    override = os.environ.get(ENV_KEY_FILE)
    if override:
        return Path(override)
    return Path.home() / ".claude-code-log" / "page.key"


def reset() -> None:
    """Forget the key read so far (for tests that move the key file)."""
    global _key
    with _lock:
        _key = None


def install_key() -> bytes:
    """The install key: read from :func:`key_path`, made there if missing.

    Published with a hard link (:func:`_make`), so that two processes
    starting at once (render workers) end up with the same key, and
    neither ever reads it half written: the loser reads the winner's. A
    file that holds no key is an error, not a reason to make a new one:
    every page made with the old key would stop working.
    """
    global _key
    with _lock:
        if _key is None:
            _key = _read_or_make(key_path())
        return _key


def _read_or_make(path: Path) -> bytes:
    if not path.exists():
        _make(path)
    text = path.read_text(encoding="ascii", errors="replace").strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = b""
    if len(key) != KEY_BYTES:
        raise ValueError(
            f"{path} does not hold a claude-code-log key "
            f"({KEY_BYTES * 2} hex digits); remove it to make a new one"
        )
    return key


def _make(path: Path) -> None:
    """Write a new key to ``path``, unless another process got there first.

    Written whole to a file of its own, then linked into place: a hard link
    fails if the name exists, so only one key is ever published, and never
    half written (a file created in place exists, empty, before its write,
    and a process reading it then took it for a damaged key).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secrets.token_bytes(KEY_BYTES).hex() + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # another process's key is in place: use it
    finally:
        tmp.unlink(missing_ok=True)


def _derive(label: str) -> bytes:
    return hmac.digest(install_key(), label.encode("utf-8"), hashlib.sha256)


def probe_token(session_id: str) -> str:
    """The token a session page's probe answers to (live_probe_post.js)."""
    return _derive("probe:" + session_id).hex()[:32]


_warned = False


def probe_token_or_none(session_id: str) -> Optional[str]:
    """:func:`probe_token`, or None when there is no key to be had (a key
    file that can't be made, as under a read-only home, or that holds no
    key). Following from disk is a convenience: without a token the page
    is written as a combined page is, static from disk, and the
    conversion goes on. Said once per process, on stderr."""
    global _warned
    try:
        return probe_token(session_id)
    except (OSError, ValueError) as exc:
        if not _warned:
            _warned = True
            import sys

            print(
                f"claude-code-log: session pages can't be followed from disk "
                f"without a key ({exc})",
                file=sys.stderr,
            )
        return None


def recent_key() -> bytes:
    """The key ``recent.html`` carries to decrypt ``recent-data.js``."""
    return _derive("recent-data")


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HKDF-SHA256 (salt: ``nonce``) in chunks of :data:`CHUNK_BYTES`, each
    with its 4-byte big-endian index as ``info``: what recent.js derives
    with ``crypto.subtle.deriveBits`` per chunk."""
    prk = hmac.digest(nonce, key, hashlib.sha256)
    out = bytearray()
    for index, start in enumerate(range(0, length, CHUNK_BYTES)):
        want = min(CHUNK_BYTES, length - start)
        info = index.to_bytes(4, "big")
        block = b""
        counter = 0
        while want > 0:
            counter += 1
            block = hmac.digest(prk, block + info + bytes([counter]), hashlib.sha256)
            out += block[:want]
            want -= len(block[:want])
    return bytes(out)


def encrypt_recent(plain: bytes) -> str:
    """``plain`` encrypted under :func:`recent_key`: base64 of a fresh
    16-byte nonce followed by ``plain`` XORed with the keystream."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    stream = _keystream(recent_key(), nonce, len(plain))
    cipher = (int.from_bytes(plain, "big") ^ int.from_bytes(stream, "big")).to_bytes(
        len(plain), "big"
    )
    return base64.b64encode(nonce + cipher).decode("ascii")


def decrypt_recent(blob: str) -> bytes:
    """The inverse of :func:`encrypt_recent` (what recent.js does), for
    tests and for reading a data file by hand."""
    raw = base64.b64decode(blob)
    nonce, cipher = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    stream = _keystream(recent_key(), nonce, len(cipher))
    return (int.from_bytes(cipher, "big") ^ int.from_bytes(stream, "big")).to_bytes(
        len(cipher), "big"
    )

"""Factory for ``type: "attachment"`` transcript entries (issue #128).

Claude Code records out-of-band events as ``attachment`` JSONL entries:
hook callbacks (``hook_success``, ``hook_blocking_error``, ...),
deferred-tool deltas, queued commands, file references, todo
reminders, and similar harness-side metadata. Until issue #128, all of
these were dropped at parse time as ``PassthroughTranscriptEntry``,
which left the user unable to inspect any hook output even at
full-detail.

This factory promotes the *hook* flavours into a renderable
``HookAttachmentMessage`` and ``queued_command`` (modern steering
deliveries) into a ``UserSteeringMessage``; the remaining non-hook
flavours still return ``None`` so they keep the historical "structural
in DAG, hidden from rendering" behaviour. New attachment flavours can
grow their own factory branch here as needed.
"""

from typing import Any, Optional, cast

from ..models import (
    AttachmentTranscriptEntry,
    HookAttachmentMessage,
    MessageContent,
    TextContent,
    UserSteeringMessage,
    UserTextMessage,
)
from .meta_factory import create_meta
from .user_factory import create_user_message


# Attachment ``type`` values produced when a Claude Code hook fires.
# Mapped to the ``kind`` discriminator on HookAttachmentMessage.
_HOOK_KINDS: dict[str, str] = {
    "hook_success": "success",
    "hook_additional_context": "additional_context",
    "hook_blocking_error": "blocking_error",
    "hook_non_blocking_error": "non_blocking_error",
}


def _stringify_content(value: Any) -> str:
    """Coerce attachment ``content`` to a string.

    ``hook_success`` and ``hook_non_blocking_error`` carry ``content``
    as a string; ``hook_additional_context`` carries it as a list of
    strings (one per line of injected prompt context). Anything else
    is fed through ``str()`` defensively.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Preserve per-element line breaks for additional_context lists.
        items = cast(list[Any], value)
        return "\n".join(str(item) for item in items)
    return str(value)


def _coerce_int(value: Any) -> Optional[int]:
    """Return value if it's an int (excluding bool), else None."""
    # bool is a subclass of int — filter it out so JSON true/false don't
    # silently become 1/0 here.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _create_queued_command_message(
    transcript: AttachmentTranscriptEntry,
    payload: dict[str, Any],
) -> Optional[MessageContent]:
    """Promote a ``queued_command`` attachment to a steering message.

    Modern Claude Code (≳2.1.101) writes a ``queued_command`` attachment
    for every steering delivery — an in-DAG, uuid'd record that is
    strictly better than the chain-less legacy queue-operation
    ``remove`` (which we suppress per-version in the renderer once a
    ``queued_command`` is seen). The steering text lives in
    ``payload["prompt"]``.

    The text is routed through :func:`create_user_message` so it gets
    the same classification + plugin-transformer pass as an
    idle-delivered user prompt: a ``[monitor] …`` steering injection
    renders as the same demoted marker as its non-steering siblings. If
    no transformer rewrites it (the result is a *plain*
    ``UserTextMessage``), it is wrapped as ``UserSteeringMessage`` to
    keep the ``user steering`` CSS treatment; a transformed/other
    classification is returned unchanged.
    """
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None

    meta = create_meta(transcript)
    chunk: list[Any] = [TextContent(type="text", text=prompt)]
    result = create_user_message(meta, chunk, prompt, is_slash_command=False)
    if result is None:
        return None

    # Only a plain, untransformed UserTextMessage becomes steering. An
    # exact-type check mirrors the legacy-path hardening in renderer.py:
    # a transformer may return a UserTextMessage *subclass* carrying its
    # content in own fields with ``items=[]`` — rebuilding steering from
    # its ``items`` would blank the card.
    if type(result) is UserTextMessage:
        return UserSteeringMessage(items=result.items, meta=meta)
    return result


def create_attachment_message(
    transcript: AttachmentTranscriptEntry,
) -> Optional[MessageContent]:
    """Build a renderable MessageContent from an attachment entry.

    Returns ``None`` for attachment flavours we don't surface yet — the
    DAG still keeps the entry as a structural node so downstream
    children resolve their parent_uuid correctly; rendering simply
    skips it. This mirrors the pre-#128 PassthroughTranscriptEntry
    behaviour.

    Args:
        transcript: Parsed attachment entry from the JSONL file.

    Returns:
        A renderable content model (``HookAttachmentMessage`` for hook
        flavours, a steering message for ``queued_command``); ``None``
        otherwise.
    """
    payload = transcript.attachment or {}

    attachment_type = payload.get("type")

    if attachment_type == "queued_command":
        return _create_queued_command_message(transcript, payload)

    kind = (
        _HOOK_KINDS.get(attachment_type) if isinstance(attachment_type, str) else None
    )
    if kind is None:
        return None

    meta = create_meta(transcript)

    # ``hook_blocking_error`` nests its payload under a ``blockingError``
    # object: ``{"blockingError": "<message>", "command": "<cmd>"}``.
    # The other hook kinds carry command/stdout/stderr at the top.
    blocking = payload.get("blockingError")
    if kind == "blocking_error" and isinstance(blocking, dict):
        blocking_dict = cast(dict[str, Any], blocking)
        blocking_text = blocking_dict.get("blockingError")
        command = blocking_dict.get("command") or payload.get("command")
        return HookAttachmentMessage(
            meta=meta,
            kind=kind,
            hook_event=str(payload.get("hookEvent") or ""),
            hook_name=str(payload.get("hookName") or ""),
            tool_use_id=payload.get("toolUseID")
            if isinstance(payload.get("toolUseID"), str)
            else None,
            command=str(command) if command else None,
            blocking_error=str(blocking_text) if blocking_text else None,
        )

    return HookAttachmentMessage(
        meta=meta,
        kind=kind,
        hook_event=str(payload.get("hookEvent") or ""),
        hook_name=str(payload.get("hookName") or ""),
        tool_use_id=payload.get("toolUseID")
        if isinstance(payload.get("toolUseID"), str)
        else None,
        command=str(payload["command"]) if payload.get("command") else None,
        exit_code=_coerce_int(payload.get("exitCode")),
        duration_ms=_coerce_int(payload.get("durationMs")),
        content=_stringify_content(payload.get("content")),
        stdout=_stringify_content(payload.get("stdout")),
        stderr=_stringify_content(payload.get("stderr")),
    )

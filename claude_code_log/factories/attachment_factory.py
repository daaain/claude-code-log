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

import logging
from typing import Any, NamedTuple, Optional, cast

from ..models import (
    AttachmentTranscriptEntry,
    ContentItem,
    HookAttachmentMessage,
    ImageContent,
    MessageContent,
    TextContent,
    UserSteeringMessage,
    UserTextMessage,
)
from ..parser import extract_text_content
from .meta_factory import create_meta
from .transcript_factory import USER_CONTENT_TYPES, create_message_content
from .user_factory import create_user_message


logger = logging.getLogger(__name__)


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


class QueuedCommandPrompt(NamedTuple):
    """A ``queued_command`` prompt normalized for rendering and pairing.

    Attributes:
        items: Content items to render. Never empty.
        pairable: Whether ``items`` yields text the steering-suppression
            budget can pair a legacy ``remove`` against. False means the
            paired ``remove`` must stay visible — see
            :func:`queued_command_prompt_items`.
        shape: The prompt shape actually seen, for diagnostics
            (``"str"``, ``"list[image,text]"``, ``"dict"``, ...).
    """

    items: list[ContentItem]
    pairable: bool
    shape: str


def _describe_prompt_shape(prompt: Any) -> str:
    """Name the shape of a ``prompt`` payload for a diagnostic message.

    A bare ``type()`` name is not enough for the list case — the useful
    detail is *which block types* it held, since that is what a future
    novel shape will differ in.
    """
    if isinstance(prompt, list):
        blocks = cast(list[Any], prompt)
        kinds: set[str] = set()
        for block in blocks:
            if isinstance(block, dict):
                block_map = cast(dict[str, Any], block)
                kinds.add(str(block_map.get("type", "?")))
            else:
                kinds.add(type(block).__name__)
        return f"list[{','.join(sorted(kinds))}]" if kinds else "list[]"
    return type(prompt).__name__


def queued_command_prompt_items(
    payload: dict[str, Any],
) -> Optional[QueuedCommandPrompt]:
    """Normalize a ``queued_command`` payload's ``prompt`` to content items.

    ``prompt`` is a plain string for a text-only steering delivery, but a
    *list* of content blocks when the steering message carries an image
    (``[{"type": "text", ...}, {"type": "image", ...}]``) — the shape a
    pasted screenshot produces. A string yields the single ``TextContent``
    the pre-list code built by hand, so text-only steering is unaffected.

    Any *other* shape is still rendered, from its ``str()``, rather than
    dropped: silently discarding a steering delivery is indistinguishable
    from there having been none. Such a prompt comes back with
    ``pairable=False`` and the caller is expected to say so out loud —
    see :func:`_create_queued_command_message`.

    ``pairable`` requires **both** a supported shape (``str`` or a list of
    blocks) and non-empty text — text because it is the only thing the
    suppression budget can key on, and shape because a ``str()``-rendered
    fallback is not something we can claim to have matched. Both conditions
    bite independently: an image-only list is renderable but not pairable,
    and a text-bearing ``dict`` is not pairable either. Either way the paired
    ``remove`` simply stays visible.

    Returns ``None`` only when there is nothing at all to render (no
    ``prompt`` key, or an empty/whitespace one) — the one case where
    dropping the card loses nothing.

    Shared by :func:`_create_queued_command_message` and the renderer's
    steering-suppression pre-pass so the two cannot disagree: the
    pre-pass seeds the budget from the ``pairable`` cards only, and every
    card the factory emits that it did *not* count leaves the paired
    legacy ``remove`` visible.
    """
    prompt = payload.get("prompt")
    if prompt is None:
        return None
    shape = _describe_prompt_shape(prompt)

    items: list[ContentItem]
    if isinstance(prompt, (str, list)):
        items = create_message_content(prompt, USER_CONTENT_TYPES)
    else:
        # Fail closed, visibly: keep the payload as text so the delivery
        # shows up in the page instead of vanishing.
        items = [TextContent(type="text", text=str(prompt))]

    text = extract_text_content(items).strip()
    if not text and not any(isinstance(item, ImageContent) for item in items):
        return None
    return QueuedCommandPrompt(
        items=items,
        pairable=bool(text) and isinstance(prompt, (str, list)),
        shape=shape,
    )


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
    prompt = queued_command_prompt_items(payload)
    if prompt is None:
        return None

    if not prompt.pairable:
        # Rendered, but the suppression pre-pass could not count it, so the
        # paired legacy ``remove`` stays visible. Name the shape we actually
        # saw: the next novel prompt shape should be diagnosable from this
        # line alone, without re-deriving it from an archive.
        logger.warning(
            "queued_command prompt shape %s is not pairable (session %s, "
            "version %s): rendering the steering card, but its legacy "
            "'remove' cannot be matched and will render too. Only a str or "
            "a list of blocks with text can be paired.",
            prompt.shape,
            transcript.sessionId,
            transcript.version,
        )

    meta = create_meta(transcript)
    result = create_user_message(
        meta, prompt.items, extract_text_content(prompt.items), is_slash_command=False
    )
    # Defensive only: create_user_message never returns None for a non-empty
    # chunk (it returns None solely on an empty content_list / the empty
    # is_slash_command path, neither reachable here). Kept to guard against a
    # future change to its contract — the effective render condition upstream
    # is exactly ``queued_command_prompt_items``, so pre-pass and factory
    # can't drift.
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

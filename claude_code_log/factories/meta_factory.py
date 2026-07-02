"""Factory for creating MessageMeta from transcript entries.

This module handles extraction of common metadata from transcript entries
that is shared across all message types.
"""

from ..models import AssistantTranscriptEntry, BaseTranscriptEntry, MessageMeta


def create_meta(transcript: BaseTranscriptEntry) -> MessageMeta:
    """Create MessageMeta from a transcript entry.

    Extracts all shared fields from BaseTranscriptEntry subclasses.

    Args:
        transcript: Any transcript entry inheriting from BaseTranscriptEntry

    Returns:
        MessageMeta with identity and context fields
    """
    return MessageMeta(
        # Identity fields
        session_id=transcript.sessionId,
        timestamp=transcript.timestamp,
        uuid=transcript.uuid,
        parent_uuid=transcript.parentUuid,
        # Context fields
        is_sidechain=transcript.isSidechain,
        is_meta=getattr(transcript, "isMeta", False) or False,
        source_tool_use_id=getattr(transcript, "sourceToolUseID", None),
        agent_id=transcript.agentId,
        spawned_agent_id=transcript.spawnedAgentId,
        cwd=transcript.cwd,
        git_branch=transcript.gitBranch,
        team_name=getattr(transcript, "teamName", None),
        # The model id lives on the assistant message body, not the base entry.
        model=(
            transcript.message.model
            if isinstance(transcript, AssistantTranscriptEntry)
            else None
        ),
    )

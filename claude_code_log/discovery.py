"""Unified session discovery across all providers."""

from typing import Iterator, Optional

from .providers import discover_providers
from .providers.base import SessionInfo


def discover_all_sessions(
    providers: Optional[list[str]] = None,
) -> Iterator[SessionInfo]:
    """Discover sessions from all available providers.

    Args:
        providers: Optional list of provider names to include.
                  If None, discovers from all available providers.

    Yields:
        SessionInfo objects from all providers.
    """
    registry = discover_providers()

    if providers is None:
        providers = registry.get_available_providers()

    for provider_name in providers:
        provider = registry.get_provider(provider_name)
        if provider and provider.is_available():
            yield from provider.discover_sessions()


def discover_sessions_by_provider(provider_name: str) -> Iterator[SessionInfo]:
    """Discover sessions from a specific provider.

    Args:
        provider_name: Name of the provider to discover sessions from.

    Yields:
        SessionInfo objects from the specified provider.
    """
    registry = discover_providers()
    yield from registry.discover_sessions_by_provider(provider_name)


def get_session_stats() -> dict[str, int]:
    registry = discover_providers()
    stats: dict[str, int] = {}

    for provider_name in registry.get_available_providers():
        provider = registry.get_provider(provider_name)
        if provider:
            count = sum(1 for _ in provider.discover_sessions())
            stats[provider_name] = count

    return stats


def load_session(provider_name: str, session_id: str):
    """Load a session from a specific provider.

    Args:
        provider_name: Name of the provider.
        session_id: ID of the session to load.

    Returns:
        Iterator of TranscriptEntry objects.
    """
    registry = discover_providers()
    return registry.load_session(provider_name, session_id)

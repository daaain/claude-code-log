"""Provider registry for auto-discovery and management."""

import logging
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Type

from .base import BaseProvider, SessionInfo

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry for managing session providers.

    Providers are registered with their data directory paths.
    Auto-discovery checks which directories exist and only enables
    providers with valid data directories.
    """

    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}
        self._provider_classes: Dict[str, Type[BaseProvider]] = {}

    def register(self, provider: BaseProvider) -> None:
        """Register a provider instance."""
        name = provider.get_provider_name()
        self._providers[name] = provider

    def register_class(self, name: str, provider_class: Type[BaseProvider]) -> None:
        """Register a provider class for lazy instantiation."""
        self._provider_classes[name] = provider_class

    def instantiate_registered(self) -> None:
        for registered_name, provider_class in sorted(self._provider_classes.items()):
            try:
                provider = provider_class()
                actual_name = provider.get_provider_name()
                if actual_name != registered_name:
                    logger.warning(
                        "Provider registered as %s reported mismatched name %s; skipping",
                        registered_name,
                        actual_name,
                    )
                    continue
                self._providers[registered_name] = provider
            except Exception as exc:
                logger.warning(
                    "Unable to initialize provider %s (%s)",
                    registered_name,
                    type(exc).__name__,
                )

    def get_provider(self, name: str) -> Optional[BaseProvider]:
        """Get a registered provider by name."""
        return self._providers.get(name)

    def get_available_providers(self) -> List[str]:
        """Get names of all available providers (with valid data directories)."""
        available: List[str] = []
        for name, provider in sorted(self._providers.items()):
            if provider.is_available():
                available.append(name)
        return available

    def get_all_providers(self) -> List[str]:
        """Get names of all registered providers."""
        return sorted(self._providers)

    def discover_all_sessions(self) -> Iterator[SessionInfo]:
        """Discover sessions from all available providers."""
        for _, provider in sorted(self._providers.items()):
            if provider.is_available():
                yield from provider.discover_sessions()

    def discover_sessions_by_provider(
        self, provider_name: str
    ) -> Iterator[SessionInfo]:
        """Discover sessions from a specific provider."""
        provider = self._providers.get(provider_name)
        if provider and provider.is_available():
            yield from provider.discover_sessions()

    def detect_provider_for_path(self, path: Path) -> Optional[str]:
        """Return the single provider name that recognizes *path* via its cheap
        ``detect_path`` sniff, or ``None`` if none do.

        Detection is independent of ``is_available`` — an INPUT_PATH rollout may
        be handed in even when the provider's own data dir is absent. If more
        than one provider claims the path the choice is ambiguous, so raise and
        tell the caller to disambiguate with ``--provider`` (DECIDED #2).
        """
        matches = [
            name
            for name, provider in sorted(self._providers.items())
            if provider.detect_path(path)
        ]
        if len(matches) > 1:
            raise ValueError(
                "INPUT_PATH matches multiple providers "
                f"({', '.join(matches)}); pass --provider to disambiguate"
            )
        return matches[0] if matches else None

    def load_session(
        self, provider_name: str, session_id: str, max_messages: Optional[int] = None
    ):
        """Load a session from a specific provider."""
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_name}")
        if not provider.is_available():
            raise ValueError(f"Provider {provider_name} is not available")
        return provider.load_session(session_id, max_messages=max_messages)


def discover_providers() -> ProviderRegistry:
    """Auto-discover available providers based on ~/. directories.

    Returns a ProviderRegistry with all available providers registered.
    """
    registry = ProviderRegistry()

    from .claude import ClaudeProvider
    from .agy import AgyProvider
    from .codex import CodexProvider

    registry.register_class("claude", ClaudeProvider)
    registry.register_class("agy", AgyProvider)
    registry.register_class("codex", CodexProvider)

    registry.instantiate_registered()

    return registry

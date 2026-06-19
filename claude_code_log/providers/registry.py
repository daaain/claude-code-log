"""Provider registry for auto-discovery and management."""

from pathlib import Path
from typing import Dict, Iterator, List, Optional, Type

from .base import BaseProvider, SessionInfo


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

    def get_provider(self, name: str) -> Optional[BaseProvider]:
        """Get a registered provider by name."""
        return self._providers.get(name)

    def get_available_providers(self) -> List[str]:
        """Get names of all available providers (with valid data directories)."""
        available = []
        for name, provider in self._providers.items():
            if provider.is_available():
                available.append(name)
        return available

    def get_all_providers(self) -> List[str]:
        """Get names of all registered providers."""
        return list(self._providers.keys())

    def discover_all_sessions(self) -> Iterator[SessionInfo]:
        """Discover sessions from all available providers."""
        for provider in self._providers.values():
            if provider.is_available():
                yield from provider.discover_sessions()

    def discover_sessions_by_provider(
        self, provider_name: str
    ) -> Iterator[SessionInfo]:
        """Discover sessions from a specific provider."""
        provider = self._providers.get(provider_name)
        if provider and provider.is_available():
            yield from provider.discover_sessions()

    def load_session(self, provider_name: str, session_id: str):
        """Load a session from a specific provider."""
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Unknown provider: {provider_name}")
        if not provider.is_available():
            raise ValueError(f"Provider {provider_name} is not available")
        return provider.load_session(session_id)


def discover_providers() -> ProviderRegistry:
    """Auto-discover available providers based on ~/. directories.

    Returns a ProviderRegistry with all available providers registered.
    """
    registry = ProviderRegistry()

    # Import provider classes here to avoid circular imports
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .gemini import GeminiProvider
    from .opencode import OpenCodeProvider
    from .agy import AgyProvider

    # Register all provider classes
    registry.register_class("claude", ClaudeProvider)
    registry.register_class("codex", CodexProvider)
    registry.register_class("gemini", GeminiProvider)
    registry.register_class("opencode", OpenCodeProvider)
    registry.register_class("agy", AgyProvider)

    # Instantiate and register providers
    for name, provider_class in registry._provider_classes.items():
        try:
            provider = provider_class()
            registry.register(provider)
        except Exception:
            # Skip providers that fail to initialize
            pass

    return registry

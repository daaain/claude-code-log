"""Integration tests for multi-provider session support."""

import json
from pathlib import Path
from typing import Iterator

import pytest

from claude_code_log.models import TranscriptEntry
from claude_code_log.providers import discover_providers
from claude_code_log.providers.base import BaseProvider, SessionInfo
from claude_code_log.providers.codex import CodexProvider
from claude_code_log.providers.gemini import GeminiProvider
from claude_code_log.providers.opencode import OpenCodeProvider


class TestProviderBase:
    """Tests for the provider base class and registry."""

    def test_discover_providers(self):
        """Test that providers can be discovered."""
        registry = discover_providers()
        assert len(registry.get_all_providers()) == 4
        assert "claude" in registry.get_all_providers()
        assert "codex" in registry.get_all_providers()
        assert "gemini" in registry.get_all_providers()
        assert "opencode" in registry.get_all_providers()

    def test_provider_availability(self):
        """Test that provider availability can be checked."""
        registry = discover_providers()
        available = registry.get_available_providers()
        assert isinstance(available, list)


class TestCodexProvider:
    """Tests for the Codex CLI provider."""

    def test_provider_name(self):
        """Test provider name."""
        provider = CodexProvider()
        assert provider.get_provider_name() == "codex"

    def test_session_format(self):
        """Test session format."""
        provider = CodexProvider()
        assert provider.get_session_format() == "jsonl"

    def test_discover_sessions(self, tmp_path: Path):
        """Test session discovery with mock data."""
        provider = CodexProvider()

        # Create mock Codex directory structure
        codex_dir = tmp_path / "2026" / "06" / "19"
        codex_dir.mkdir(parents=True)

        # Create sample rollout file
        rollout_file = codex_dir / "rollout-sample.jsonl"
        with open(rollout_file, "w") as f:
            f.write(
                '{"timestamp":"2026-06-19T10:00:00.000Z","type":"session_meta","payload":{}}\n'
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            sessions = list(provider.discover_sessions())
            assert len(sessions) == 1
            assert sessions[0].provider == "codex"
        finally:
            provider.get_data_dir = original_get_data_dir

    def test_load_session(self, tmp_path: Path):
        """Test session loading with mock data."""
        provider = CodexProvider()

        # Create mock Codex directory structure
        codex_dir = tmp_path / "2026" / "06" / "19"
        codex_dir.mkdir(parents=True)

        # Create sample rollout file
        rollout_file = codex_dir / "test-session.jsonl"
        with open(rollout_file, "w") as f:
            f.write(
                '{"timestamp":"2026-06-19T10:00:00.000Z","type":"session_meta","payload":{}}\n'
            )
            f.write(
                '{"timestamp":"2026-06-19T10:00:01.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Hello!"}]}}\n'
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            entries = list(provider.load_session("test-session"))
            assert len(entries) > 0
        finally:
            provider.get_data_dir = original_get_data_dir


class TestGeminiProvider:
    """Tests for the Gemini CLI provider."""

    def test_provider_name(self):
        """Test provider name."""
        provider = GeminiProvider()
        assert provider.get_provider_name() == "gemini"

    def test_session_format(self):
        """Test session format."""
        provider = GeminiProvider()
        assert provider.get_session_format() == "jsonl"

    def test_discover_sessions(self, tmp_path: Path):
        """Test session discovery with mock data."""
        provider = GeminiProvider()

        # Create mock Gemini directory structure
        chats_dir = tmp_path / "project123" / "chats"
        chats_dir.mkdir(parents=True)

        # Create sample session file
        session_file = chats_dir / "session-sample.jsonl"
        with open(session_file, "w") as f:
            f.write(
                '{"sessionId":"session-123","projectHash":"abc123","startTime":"2026-06-19T10:00:00.000Z","messages":[]}\n'
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            sessions = list(provider.discover_sessions())
            assert len(sessions) == 1
            assert sessions[0].provider == "gemini"
        finally:
            provider.get_data_dir = original_get_data_dir

    def test_load_session(self, tmp_path: Path):
        """Test session loading with mock data."""
        provider = GeminiProvider()

        # Create mock Gemini directory structure
        chats_dir = tmp_path / "project123" / "chats"
        chats_dir.mkdir(parents=True)

        # Create sample session file
        session_file = chats_dir / "test-session.jsonl"
        with open(session_file, "w") as f:
            f.write(
                '{"sessionId":"test-session","projectHash":"abc123","startTime":"2026-06-19T10:00:00.000Z","messages":[{"type":"user","id":"msg-001","timestamp":"2026-06-19T10:00:00.000Z","content":"Hello"},{"type":"gemini","id":"msg-002","timestamp":"2026-06-19T10:00:01.000Z","content":"Hi there!"}]}\n'
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            entries = list(provider.load_session("test-session"))
            assert len(entries) == 2
        finally:
            provider.get_data_dir = original_get_data_dir


class TestOpenCodeProvider:
    """Tests for the OpenCode provider."""

    def test_provider_name(self):
        """Test provider name."""
        provider = OpenCodeProvider()
        assert provider.get_provider_name() == "opencode"

    def test_session_format(self):
        """Test session format."""
        provider = OpenCodeProvider()
        assert provider.get_session_format() == "json"

    def test_discover_sessions(self, tmp_path: Path):
        """Test session discovery with mock data."""
        provider = OpenCodeProvider()

        # Create mock OpenCode directory structure
        session_dir = tmp_path / "session" / "test-project"
        session_dir.mkdir(parents=True)

        # Create sample session file
        session_file = session_dir / "test-session.json"
        with open(session_file, "w") as f:
            json.dump(
                {
                    "id": "test-session",
                    "projectID": "test-project",
                    "title": "Test Session",
                    "time": {"created": 1750310400000, "updated": 1750310700000},
                },
                f,
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            sessions = list(provider.discover_sessions())
            assert len(sessions) == 1
            assert sessions[0].provider == "opencode"
        finally:
            provider.get_data_dir = original_get_data_dir

    def test_load_session(self, tmp_path: Path):
        """Test session loading with mock data."""
        provider = OpenCodeProvider()

        # Create mock OpenCode directory structure
        session_dir = tmp_path / "session" / "test-project"
        session_dir.mkdir(parents=True)

        message_dir = tmp_path / "message" / "test-session"
        message_dir.mkdir(parents=True)

        part_dir = tmp_path / "part" / "msg-001"
        part_dir.mkdir(parents=True)

        # Create sample files
        with open(session_dir / "test-session.json", "w") as f:
            json.dump(
                {
                    "id": "test-session",
                    "projectID": "test-project",
                    "title": "Test Session",
                    "time": {"created": 1750310400000, "updated": 1750310700000},
                },
                f,
            )

        with open(message_dir / "msg-001.json", "w") as f:
            json.dump(
                {
                    "id": "msg-001",
                    "sessionID": "test-session",
                    "role": "user",
                    "time": {"created": 1750310400000},
                },
                f,
            )

        with open(part_dir / "part-001.json", "w") as f:
            json.dump(
                {
                    "id": "part-001",
                    "sessionID": "test-session",
                    "messageID": "msg-001",
                    "type": "text",
                    "text": "Hello!",
                },
                f,
            )

        # Override get_data_dir to use our test directory
        original_get_data_dir = provider.get_data_dir
        provider.get_data_dir = lambda: tmp_path

        try:
            entries = list(provider.load_session("test-session"))
            assert len(entries) > 0
        finally:
            provider.get_data_dir = original_get_data_dir


class TestDiscovery:
    """Tests for unified session discovery."""

    def test_discover_all_sessions(self):
        """Test that sessions can be discovered from all providers."""
        from claude_code_log.discovery import discover_all_sessions

        sessions = list(discover_all_sessions())
        assert isinstance(sessions, list)

    def test_get_session_stats(self):
        """Test that session statistics can be retrieved."""
        from claude_code_log.discovery import get_session_stats

        stats = get_session_stats()
        assert isinstance(stats, dict)
        assert all(isinstance(v, int) for v in stats.values())

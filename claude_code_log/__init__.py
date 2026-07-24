#!/usr/bin/env python3
"""
claude-code-log - Convert Claude Code transcripts to HTML/Markdown.

Library API (stable):
    from claude_code_log import api
    api.load_transcript(...)
    api.CacheManager(...)

CLI:
    uvx claude-code-log@latest --open-browser
"""

# Public library API - stable interface for external consumers
from . import api

__all__ = ["api"]

# Version - try to get from package metadata
try:
    from importlib.metadata import version

    __version__ = version("claude-code-log")
except Exception:
    __version__ = "unknown"

"""Pytest configuration and shared fixtures."""

import tempfile
from pathlib import Path

import pytest

from test.snapshot_serializers import NormalisedHTMLSerializer


@pytest.fixture(autouse=True)
def temp_sqlite_db(tmp_path):
    """Set up temporary SQLite database for each test.

    This fixture runs automatically for all tests and ensures each test
    gets an isolated database.
    """
    from claude_code_log.cache import CacheManager

    # Create a unique temp database path for this test
    db_path = tmp_path / "test_cache.db"

    # Set the class-level database path
    CacheManager.set_db_path(db_path)

    yield db_path

    # Cleanup: close connections and reset
    CacheManager.close_all_connections()
    CacheManager._db_initialized = False


@pytest.fixture
def test_data_dir() -> Path:
    """Return path to test data directory."""
    return Path(__file__).parent / "test_data"


@pytest.fixture
def html_snapshot(snapshot):
    """Snapshot fixture with HTML normalisation for regression testing."""
    return snapshot.use_extension(NormalisedHTMLSerializer)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Configure browser launch arguments."""
    return {
        **browser_type_launch_args,
        "headless": True,  # Set to False for debugging
        "slow_mo": 0,  # Add delay for debugging
    }

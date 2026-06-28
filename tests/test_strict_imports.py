"""Tests for sys.meta_path import guard (Layer 2 strict mode)."""
import sys

import pytest

from godel._strict_imports import install_import_guard, remove_import_guard
from godel._exceptions import GodelStrictError


@pytest.fixture(autouse=True)
def cleanup_guard():
    """Ensure guard is removed after each test."""
    yield
    remove_import_guard()


def test_blocks_banned_module():
    sys.modules.pop("requests", None)
    install_import_guard()
    with pytest.raises(GodelStrictError) as exc_info:
        import requests  # noqa: F401
    assert exc_info.value.violations[0].layer == "import"
    assert "requests" in exc_info.value.violations[0].message


def test_blocks_banned_network_module():
    sys.modules.pop("httpx", None)
    install_import_guard()
    import importlib
    with pytest.raises(GodelStrictError):
        importlib.import_module("httpx")


def test_allows_stdlib():
    install_import_guard()
    import json  # noqa: F401
    import os  # noqa: F401


def test_remove_guard():
    install_import_guard()
    remove_import_guard()
    from godel._strict_imports import _blocker_instance
    assert _blocker_instance is None


def test_idempotent_install():
    install_import_guard()
    install_import_guard()
    blockers = [f for f in sys.meta_path if f.__class__.__name__ == "_GodelImportBlocker"]
    assert len(blockers) == 1
    remove_import_guard()

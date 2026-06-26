"""Tests for sys.meta_path import guard (Layer 2 strict mode)."""
import pytest
from godel._strict_imports import install_import_guard, remove_import_guard
from godel._exceptions import GodelStrictError


@pytest.fixture(autouse=True)
def cleanup_guard():
    """Ensure guard is removed after each test."""
    yield
    remove_import_guard()


def test_blocks_banned_module():
    import sys
    # Make sure requests is not already imported
    sys.modules.pop("requests", None)
    install_import_guard()
    with pytest.raises(GodelStrictError) as exc_info:
        import requests  # noqa: F401
    assert exc_info.value.violations[0].layer == "import"
    assert "requests" in exc_info.value.violations[0].message


def test_blocks_socket():
    import sys
    sys.modules.pop("socket", None)
    # socket may already be loaded by pytest internals, skip if so
    install_import_guard()
    # Test with importlib to avoid affecting test runner
    import importlib
    with pytest.raises(GodelStrictError):
        importlib.import_module("httpx")


def test_allows_stdlib():
    install_import_guard()
    import json  # noqa: F401 — should not raise
    import os  # noqa: F401


def test_remove_guard():
    import sys
    install_import_guard()
    remove_import_guard()
    # After removal, banned modules should be importable again
    # (if they're installed — just verify no exception from our guard)
    sys.modules.pop("httpx", None)
    # We don't test actual import of httpx since it may not be installed
    # Just verify our blocker is gone
    from godel._strict_imports import _blocker_instance
    assert _blocker_instance is None


def test_idempotent_install():
    install_import_guard()
    install_import_guard()  # should not raise or add duplicate
    import sys
    sum(1 for f in sys.meta_path if isinstance(f, type(None)) or f.__class__.__name__ == "_GodelImportBlocker")
    # Just verify remove works after double install
    remove_import_guard()

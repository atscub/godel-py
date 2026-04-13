"""Tests for the godel[watch] optional dependency guard.

Verifies that:
- Importing godel._watch without rich raises GodelWatchNotInstalledError with
  an actionable message.
- The CLI exits with a non-zero exit code and prints the actionable message
  (no Python traceback) when --watch is requested without rich installed.
"""
from __future__ import annotations

import importlib
import sys
import types
import os
import subprocess

import pytest

from godel._exceptions import GodelWatchNotInstalledError


# ---------------------------------------------------------------------------
# Unit tests — monkeypatched import
# ---------------------------------------------------------------------------

def test_watch_import_raises_when_rich_missing(monkeypatch):
    """GodelWatchNotInstalledError is raised when rich is absent."""
    # Remove rich from sys.modules if present so the import is attempted fresh
    monkeypatch.delitem(sys.modules, "rich", raising=False)
    # Remove _watch from sys.modules so it re-executes top-level code
    monkeypatch.delitem(sys.modules, "godel._watch", raising=False)

    # Inject a broken 'rich' finder that raises ImportError
    class _BlockRich:
        @classmethod
        def find_spec(cls, fullname, path, target=None):
            if fullname == "rich" or fullname.startswith("rich."):
                raise ImportError("rich blocked for testing")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockRich()] + sys.meta_path)

    with pytest.raises(GodelWatchNotInstalledError) as exc_info:
        import godel._watch  # noqa: F401

    msg = str(exc_info.value)
    assert "pip install 'godel[watch]'" in msg
    assert "rich" in msg


def test_watch_error_message_content(monkeypatch):
    """The error message contains both 'rich' and the install instruction."""
    monkeypatch.delitem(sys.modules, "rich", raising=False)
    monkeypatch.delitem(sys.modules, "godel._watch", raising=False)

    class _BlockRich:
        @classmethod
        def find_spec(cls, fullname, path, target=None):
            if fullname == "rich" or fullname.startswith("rich."):
                raise ImportError("rich blocked for testing")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockRich()] + sys.meta_path)

    with pytest.raises(GodelWatchNotInstalledError) as exc_info:
        import godel._watch  # noqa: F401

    msg = str(exc_info.value)
    assert "godel --watch" in msg
    assert "pip install 'godel[watch]'" in msg


def test_godelwatchnotinstallederror_is_import_error():
    """GodelWatchNotInstalledError is an ImportError subclass."""
    err = GodelWatchNotInstalledError("test")
    assert isinstance(err, ImportError)


# ---------------------------------------------------------------------------
# CLI integration test — subprocess, no rich in PATH
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PYTHON = sys.executable


def test_cli_watch_flag_without_rich_exits_nonzero(tmp_path, monkeypatch):
    """godel run --watch without rich: exits non-zero, prints actionable message,
    no Python traceback."""
    workflow_file = os.path.join(FIXTURES, "good_workflow.py")

    # Run in a subprocess with a shim that makes `import rich` fail.
    # We inject a small sitecustomize-style wrapper via PYTHONSTARTUP is not
    # available for -c, so we use a wrapper script instead.
    shim = tmp_path / "block_rich.pth"
    blocker_src = tmp_path / "_block_rich_sitecustomize.py"
    blocker_src.write_text(
        "import sys\n"
        "class _Blocker:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path, target=None):\n"
        "        if name == 'rich' or name.startswith('rich.'):\n"
        "            raise ImportError('rich blocked for testing')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
    )
    # Use PYTHONPATH to inject a sitecustomize that blocks rich
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")

    # Write a sitecustomize.py in tmp_path that blocks rich
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import sys\n"
        "class _Blocker:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path, target=None):\n"
        "        if name == 'rich' or name.startswith('rich.'):\n"
        "            raise ImportError('rich blocked for testing')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
    )

    result = subprocess.run(
        [PYTHON, "-m", "godel", "run", "--watch", workflow_file],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "pip install 'godel[watch]'" in combined, (
        f"Expected install hint in output. Got: {combined!r}"
    )
    assert "Traceback" not in combined, (
        f"Expected no Python traceback. Got: {combined!r}"
    )

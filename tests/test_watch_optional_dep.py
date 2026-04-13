"""Tests for the godel[watch] optional dependency guard.

Verifies that:
- Importing ``godel._watch`` without ``rich`` raises
  :class:`GodelWatchNotInstalledError` with an actionable message (AC1).
- When ``rich`` IS installed, ``godel._watch`` imports cleanly — the guard
  is transparent (AC2).
- The CLI exits with a non-zero code and prints the actionable message
  (no Python traceback) when ``--watch`` is used without ``rich`` (AC3).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from godel._exceptions import GodelWatchNotInstalledError


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# AC1 — unit: monkeypatched import raises actionable error
# ---------------------------------------------------------------------------

def test_watch_import_raises_when_rich_missing(monkeypatch):
    """GodelWatchNotInstalledError is raised with actionable message when
    rich is absent."""
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
    # All required fragments of the actionable message
    assert "rich" in msg
    assert "godel --watch" in msg
    assert "pip install 'godel[watch]'" in msg


def test_godelwatchnotinstallederror_is_import_error():
    """GodelWatchNotInstalledError is an ImportError subclass."""
    assert issubclass(GodelWatchNotInstalledError, ImportError)


# ---------------------------------------------------------------------------
# AC2 — happy path: with rich installed, _watch imports without raising
# ---------------------------------------------------------------------------

def test_watch_import_succeeds_when_rich_present():
    """When rich IS installed (as in the dev env), godel._watch imports
    cleanly and the guard is a no-op."""
    pytest.importorskip("rich")
    # Force a fresh import to exercise the top-level try/except path.
    sys.modules.pop("godel._watch", None)
    import godel._watch  # noqa: F401
    # Module attribute check — module loaded fully
    assert "godel._watch" in sys.modules


# ---------------------------------------------------------------------------
# AC3 — CLI integration: --watch without rich exits cleanly (no traceback)
# ---------------------------------------------------------------------------

def test_cli_watch_flag_without_rich_exits_nonzero(tmp_path):
    """godel run --watch without rich: exits non-zero, prints actionable
    message, no Python traceback."""
    workflow_file = os.path.join(FIXTURES, "good_workflow.py")

    # sitecustomize.py in PYTHONPATH runs at interpreter startup, before
    # any `import rich` attempt — installs a meta_path finder that blocks rich.
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

    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")

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

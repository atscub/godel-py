"""Live-watch rendering for workflow runs.

This module requires the ``watch`` optional dependency (``rich``).  Import is
guarded so that ``godel`` core remains importable without the extra installed.

Install with::

    pip install 'godel[watch]'
"""
from __future__ import annotations

from godel._exceptions import GodelWatchNotInstalledError

try:
    import rich  # noqa: F401
except ImportError as _e:
    raise GodelWatchNotInstalledError(
        "godel --watch requires 'rich'. Install with: pip install 'godel[watch]'"
    ) from _e


def _check_watch_available() -> None:
    """No-op when called after a successful import — used by the CLI to
    trigger the import-time guard in a controlled location."""

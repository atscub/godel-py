"""Layer 2: sys.meta_path import guard for strict mode."""
from __future__ import annotations

import importlib.abc
import sys

from godel._strict_ast import BANNED_MODULES


class _GodelImportBlocker(importlib.abc.MetaPathFinder):
    """Blocks import of banned modules at runtime."""

    def find_module(self, fullname: str, path=None):
        top_level = fullname.split(".")[0]
        if fullname in BANNED_MODULES or top_level in BANNED_MODULES:
            return self
        return None

    def load_module(self, fullname: str):
        from godel._exceptions import GodelStrictError, StrictViolation
        raise GodelStrictError(violations=[
            StrictViolation(
                file="<runtime>",
                line=0, col=0,
                message=f"banned module import at runtime: {fullname}",
                layer="import",
            )
        ])


_blocker_instance: _GodelImportBlocker | None = None


def install_import_guard():
    """Install the import blocker. Idempotent."""
    global _blocker_instance
    if _blocker_instance is not None:
        return
    _blocker_instance = _GodelImportBlocker()
    sys.meta_path.insert(0, _blocker_instance)


def remove_import_guard():
    """Remove the import blocker. For testing."""
    global _blocker_instance
    if _blocker_instance is None:
        return
    sys.meta_path.remove(_blocker_instance)
    _blocker_instance = None

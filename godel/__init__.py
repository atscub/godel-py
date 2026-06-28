"""Godel — deterministic orchestrator for AI agent workflows."""
__version__ = "3.16.7"

import sys
from pathlib import Path


def version() -> str:
    """Return the package version string from pyproject.toml."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    if sys.version_info >= (3, 11):
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    else:
        import tomli  # type: ignore[import]
        with open(pyproject, "rb") as f:
            data = tomli.load(f)
    return data["project"]["version"]


from godel._decorators import workflow, step, WorkflowFail, parallel, retry
from godel._run import run, CommandResult, CommandFailure, ContextOverflowError
from godel.io import print, input, sleep, read_text, write_text
from godel._events import Event, EventStatus
from godel._event_log import EventLog
from godel._context import get_event_log
from godel._exceptions import (
    GodelStrictError,
    StrictViolation,
    ResumeError,
    UnsafeResumeError,
    SourceEditedError,
    RewindSignal,
    PauseSignal,
    GodelError,
    AgentRefusal,
    SchemaValidationFailure,
    HumanTimeout,
    NonDeterministicEscape,
    RewindUnsafe,
    GodelWatchNotInstalledError,
    ConfigError,
    StepTimeout,
)
from godel._pause import check_pause_request, write_pause_request, clear_pause_request, pause
from godel._config import load_config, GodelConfig, LoadedConfig, resolve_workflow, list_workflows, open_event_log
from godel._rewind import rewind
from godel._tail import tail
from godel import det

__all__ = [
    "workflow",
    "step",
    "WorkflowFail",
    "parallel",
    "retry",
    "run",
    "CommandResult",
    "CommandFailure",
    "print",
    "input",
    "sleep",
    "read_text",
    "write_text",
    "Event",
    "EventStatus",
    "EventLog",
    "get_event_log",
    "GodelStrictError",
    "StrictViolation",
    "ResumeError",
    "UnsafeResumeError",
    "SourceEditedError",
    "RewindSignal",  # internal control-flow signal; exported for isinstance checks in tests
    "rewind",
    "det",
    "GodelError",
    "AgentRefusal",
    "SchemaValidationFailure",
    "HumanTimeout",
    "NonDeterministicEscape",
    "RewindUnsafe",
    "GodelWatchNotInstalledError",
    "ConfigError",
    "StepTimeout",
    "PauseSignal",
    "check_pause_request",
    "write_pause_request",
    "clear_pause_request",
    "pause",
    "tail",
    "version",
    "load_config",
    "GodelConfig",
    "LoadedConfig",
    "resolve_workflow",
    "list_workflows",
    "open_event_log",
]

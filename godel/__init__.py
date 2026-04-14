"""Godel — deterministic orchestrator for AI agent workflows."""
__version__ = "1.8.0"

from godel._decorators import workflow, step, WorkflowFail, parallel, retry
from godel._run import run, CommandResult, CommandFailure
from godel.io import print, input
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
)
from godel._pause import check_pause_request, write_pause_request, clear_pause_request, pause
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
    "PauseSignal",
    "check_pause_request",
    "write_pause_request",
    "clear_pause_request",
    "pause",
    "tail",
]

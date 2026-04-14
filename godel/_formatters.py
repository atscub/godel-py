"""Formatter registry for ``godel tail``.

Each event op can register a one-line formatter via ``@register("op.name")``.
Unknown ops fall back to ``_default_formatter``.

Usage::

    from godel._formatters import FORMATTERS

    line = FORMATTERS.get(event.op, _default_formatter)(event)
"""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from godel._events import Event

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

FORMATTERS: dict[str, Callable[["Event"], str]] = {}


def register(op_name: str) -> Callable:
    """Decorator: register a formatter for *op_name*.

    Example::

        @register("agent.thought")
        def _fmt_agent_thought(event: Event) -> str:
            return f"[{event.event_id[:8]}] agent.thought  ..."
    """
    def decorator(fn: Callable[["Event"], str]) -> Callable[["Event"], str]:
        FORMATTERS[op_name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _duration_str(event: "Event") -> str:
    """Return a formatted duration string or empty string."""
    if event.ts_end and event.ts_start:
        from datetime import datetime
        try:
            t0 = datetime.fromisoformat(event.ts_start)
            t1 = datetime.fromisoformat(event.ts_end)
            dur = (t1 - t0).total_seconds()
            return f"  ({dur:.3f}s)"
        except ValueError:
            pass
    return ""


def _step_str(event: "Event") -> str:
    """Return a slash-joined step path or '(root)'."""
    return "/".join(event.step_path) if event.step_path else "(root)"


def _base_line(event: "Event") -> str:
    """Standard layout used by most formatters."""
    return (
        f"[{event.event_id[:8]}] {event.op:<20} {_step_str(event):<30}"
        f" {event.status.value}{_duration_str(event)}"
    )


# ---------------------------------------------------------------------------
# Default fallback (unknown ops — no ?op noise)
# ---------------------------------------------------------------------------

def _default_formatter(event: "Event") -> str:
    """Fallback for unregistered ops.

    Renders: ``op  step/path  STATUS  (Xs)``
    No ``?op`` prefix, no noise.
    """
    return _base_line(event)


# ---------------------------------------------------------------------------
# Canonical formatters
# ---------------------------------------------------------------------------

@register("WORKFLOW_STARTED")
def _fmt_workflow_started(event: "Event") -> str:
    return _base_line(event)


@register("step.enter")
def _fmt_step_enter(event: "Event") -> str:
    return _base_line(event)


@register("step.exit")
def _fmt_step_exit(event: "Event") -> str:
    return _base_line(event)


@register("run")
def _fmt_run(event: "Event") -> str:
    return _base_line(event)


@register("agent.call")
def _fmt_agent_call(event: "Event") -> str:
    return _base_line(event)


@register("FORK")
def _fmt_fork(event: "Event") -> str:
    return _base_line(event)


@register("JOIN")
def _fmt_join(event: "Event") -> str:
    return _base_line(event)


@register("REWIND")
def _fmt_rewind(event: "Event") -> str:
    return _base_line(event)


@register("PAUSED")
def _fmt_paused(event: "Event") -> str:
    return _base_line(event)


@register("print")
def _fmt_print(event: "Event") -> str:
    return _base_line(event)


@register("input")
def _fmt_input(event: "Event") -> str:
    return _base_line(event)


@register("det.now")
def _fmt_det_now(event: "Event") -> str:
    return _base_line(event)


@register("det.random")
def _fmt_det_random(event: "Event") -> str:
    return _base_line(event)


@register("det.uuid4")
def _fmt_det_uuid4(event: "Event") -> str:
    return _base_line(event)


@register("UNRECOVERABLE")
def _fmt_unrecoverable(event: "Event") -> str:
    return _base_line(event)

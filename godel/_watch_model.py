"""WatchModel — pure, frozen-dataclass model of live run state.

This is the unit-testable core of the TUI observability stack.  It contains
zero rendering logic: a separate renderer observes the model.

The public contract is the :func:`reduce` function::

    new_model = reduce(model, event)

``reduce`` is a pure function.  It returns a *new* ``WatchModel`` instance for
every state-changing event; the input model is never mutated (enforced by
``frozen=True`` on all dataclasses).

Handled ops
-----------
- ``step.enter``      — upsert a StepNode with status "running"
- ``step.exit``       — mark a StepNode finished (status from event or "done")
- ``agent.thought``   — append line to the stream panel for stream_path
- ``agent.tool_call`` — append line to the stream panel for stream_path
- ``agent.tool_result``— append line to the stream panel for stream_path
- ``agent.raw``       — append line to the stream panel for stream_path
- ``stdout``          — append line to the stream panel for stream_path
- ``rotate``          — no-op (file rotation sentinel; reader concern)
- header events       — handled via ``reduce_header``; updates ``run_meta``
- Any unknown op      — no-op (forward-compatible)

Ring buffer
-----------
Each :class:`StreamPanel` keeps the last *N* lines, where *N* defaults to 200
(configurable via ``WatchModel.ring_size``).  Older lines are evicted when the
buffer is full.

Burst coalescing is NOT done here; that is ticket godel-py-5pl.11's concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping


# An empty read-only mapping — used as the default for frozen fields.
# Sharing a single instance is safe because MappingProxyType is immutable.
_EMPTY_MAP: Mapping = MappingProxyType({})


def _freeze_mapping(d: dict) -> Mapping:
    """Wrap a dict in a read-only MappingProxyType view.

    Callers must not retain a reference to the underlying dict after this
    function returns — doing so would defeat the read-only guarantee.  All
    call sites in this module build a fresh dict immediately before wrapping.
    """
    return MappingProxyType(d)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepNode:
    """Snapshot of a single workflow step's state.

    Parameters
    ----------
    path:
        Hierarchical address of the step, e.g. ``("fetch_data", "call_api")``.
    status:
        One of ``"pending"``, ``"running"``, ``"done"``, ``"failed"``.
    started_at:
        ISO-8601 timestamp when the step was entered, or ``None``.
    finished_at:
        ISO-8601 timestamp when the step exited, or ``None``.
    children:
        Ordered tuple of child ``StepNode`` values.  Immutable; replaced on
        each mutation.
    """

    path: tuple[str, ...]
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    children: tuple["StepNode", ...] = ()


@dataclass(frozen=True)
class StreamPanel:
    """Ring-buffer panel for output from one stream path.

    Parameters
    ----------
    stream_path:
        Hierarchical stream address, e.g. ``("agent", "claude")``.
    ring:
        Tuple of the last *N* output lines, oldest first.
    last_event_ts:
        ISO-8601 timestamp of the most recent event, or ``None``.
    """

    stream_path: tuple[str, ...]
    ring: tuple[str, ...] = ()
    last_event_ts: str | None = None


@dataclass(frozen=True)
class WatchModel:
    """Snapshot of the observable run state at a point in time.

    Parameters
    ----------
    run_meta:
        Header metadata from the transcript (run_id, started_at, etc.).
    steps:
        Mapping from step-path tuple to :class:`StepNode`.
    panels:
        Mapping from stream-path tuple to :class:`StreamPanel`.
    ring_size:
        Maximum number of lines retained per :class:`StreamPanel`.
        Default: 200.
    """

    run_meta: Mapping[str, object] = field(default_factory=lambda: _EMPTY_MAP)
    steps: Mapping[tuple, StepNode] = field(default_factory=lambda: _EMPTY_MAP)
    panels: Mapping[tuple, StreamPanel] = field(default_factory=lambda: _EMPTY_MAP)
    ring_size: int = 200

    @staticmethod
    def empty(*, ring_size: int = 200) -> "WatchModel":
        """Return a fresh, empty model."""
        return WatchModel(ring_size=ring_size)


# ---------------------------------------------------------------------------
# Pure reducer
# ---------------------------------------------------------------------------

def reduce(model: WatchModel, event: dict) -> WatchModel:
    """Apply *event* to *model* and return a new :class:`WatchModel`.

    This is a pure function.  The input *model* is never mutated.

    Parameters
    ----------
    model:
        Current model state.
    event:
        A parsed event dict.  The ``"op"`` key identifies the handler.
        All other keys are op-specific.

    Returns
    -------
    WatchModel
        A new model instance reflecting the state after the event.  If the
        op is unknown or the event carries no state-changing information, the
        **same** model object is returned (identity, not a copy).
    """
    op = event.get("op", "")

    if op == "step.enter":
        return _handle_step_enter(model, event)
    if op == "step.exit":
        return _handle_step_exit(model, event)
    if op in ("agent.thought", "agent.tool_call", "agent.tool_result", "agent.raw", "stdout"):
        return _handle_stream_line(model, event)
    if op == "rotate":
        # File-rotation sentinel — no model state changes.
        return model
    # Unknown op — no-op for forward-compatibility.
    return model


def reduce_header(model: WatchModel, header: dict) -> WatchModel:
    """Apply a transcript *header* dict to *model*.

    This is separate from :func:`reduce` because header lines use a different
    top-level key (``"header"``) than event lines (``"event"``).

    Parameters
    ----------
    model:
        Current model state.
    header:
        The value of the ``"header"`` key from line 1 of a transcript file,
        e.g. ``{"v": 1, "run_id": "...", "started_at": "..."}``.

    Returns
    -------
    WatchModel
        New model with ``run_meta`` updated.
    """
    merged = {**model.run_meta, **header}
    return replace(model, run_meta=_freeze_mapping(merged))


# ---------------------------------------------------------------------------
# Internal handlers
# ---------------------------------------------------------------------------

def _handle_step_enter(model: WatchModel, event: dict) -> WatchModel:
    """Upsert a step node with status 'running'."""
    path = tuple(event.get("step_path", []))
    if not path:
        return model
    ts = event.get("ts")
    existing = model.steps.get(path)
    if existing is not None:
        node = replace(existing, status="running", started_at=ts or existing.started_at)
    else:
        node = StepNode(path=path, status="running", started_at=ts)

    new_steps = {**model.steps, path: node}
    return replace(model, steps=_freeze_mapping(new_steps))


def _handle_step_exit(model: WatchModel, event: dict) -> WatchModel:
    """Mark a step node finished."""
    path = tuple(event.get("step_path", []))
    if not path:
        return model
    ts = event.get("ts")
    status = event.get("status", "done")
    existing = model.steps.get(path)
    if existing is not None:
        node = replace(existing, status=status, finished_at=ts)
    else:
        node = StepNode(path=path, status=status, finished_at=ts)

    new_steps = {**model.steps, path: node}
    return replace(model, steps=_freeze_mapping(new_steps))


def _handle_stream_line(model: WatchModel, event: dict) -> WatchModel:
    """Append a line to the appropriate StreamPanel ring buffer.

    Events missing a ``stream_path`` (or carrying an empty/null one) are
    skipped — routing a line to a ``()`` panel key would pollute the model
    with a junk drawer of un-addressable lines.  This matches the reader
    contract that stream output always belongs to *some* named stream.
    """
    raw_sp = event.get("stream_path")
    if not raw_sp:
        return model
    stream_path = tuple(raw_sp)
    # Derive a displayable line from the event.
    line = _event_to_line(event)
    ts = event.get("ts")

    existing = model.panels.get(stream_path)
    if existing is None:
        existing = StreamPanel(stream_path=stream_path)

    # Append and evict oldest if over ring_size.
    new_ring = existing.ring + (line,)
    if len(new_ring) > model.ring_size:
        new_ring = new_ring[len(new_ring) - model.ring_size:]

    new_panel = replace(existing, ring=new_ring, last_event_ts=ts or existing.last_event_ts)
    new_panels = {**model.panels, stream_path: new_panel}
    return replace(model, panels=_freeze_mapping(new_panels))


def _event_to_line(event: dict) -> str:
    """Convert an event dict to a single display line for the ring buffer.

    The mapping is intentionally simple — rendering concerns (colours, layout)
    belong to the TUI renderer, not here.
    """
    op = event.get("op", "")
    if op == "agent.thought":
        return event.get("text", "")
    if op == "agent.tool_call":
        tool = event.get("tool", "")
        inp = event.get("input", "")
        return f"[tool_call] {tool}: {inp}"
    if op == "agent.tool_result":
        tool = event.get("tool", "")
        out = event.get("output", "")
        return f"[tool_result] {tool}: {out}"
    if op == "agent.raw":
        return event.get("text", event.get("line", ""))
    if op == "stdout":
        return event.get("line", event.get("text", ""))
    # Fallback: stringify the whole event (should not happen for handled ops).
    return str(event)

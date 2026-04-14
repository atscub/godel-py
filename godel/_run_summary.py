"""Lightweight run summary scanner for `godel runs list`."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from godel._events import Event, EventStatus


def _classify_run_state(events: list[Event]) -> str:
    """Classify the run state from the event list.

    Mirrors intervention/_context.py::_classify_run_state but lives here to
    avoid importing heavy intervention machinery for a read-only listing.
    """
    non_invalidated = [e for e in events if e.status != EventStatus.INVALIDATED]

    for ev in non_invalidated:
        if ev.op == "WORKFLOW_STARTED":
            if ev.status == EventStatus.FINISHED:
                return "FINISHED"
            if ev.status == EventStatus.PAUSED:
                return "PAUSED"

    paused_events = [e for e in non_invalidated if e.op == "PAUSED"]
    if paused_events:
        return "PAUSED"

    leaf_ops = {"step.exit", "input", "notify", "prompt"}
    for ev in reversed(non_invalidated):
        if ev.op in leaf_ops or (ev.op not in ("WORKFLOW_STARTED", "PAUSED", "REWIND")):
            if ev.status == EventStatus.FAILED:
                return "FAILED"
            break

    input_events = [e for e in non_invalidated if e.op == "input"]
    for ev in input_events:
        if ev.status == EventStatus.STARTED:
            return "PAUSED"

    return "RUNNING"


@dataclass
class RunSummary:
    run_id: str
    workflow_name: str
    status: str
    ts_start: str
    duration_s: float | None


def _load_events_readonly(jsonl_path: Path) -> list[Event]:
    """Read events from a JSONL file without opening for append."""
    events: list[Event] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                events.append(Event.from_dict(d))
            except Exception:
                continue
    return events


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def summarize_run(jsonl_path: Path) -> RunSummary:
    """Return a RunSummary for the run stored at *jsonl_path*."""
    run_id = jsonl_path.stem
    try:
        events = _load_events_readonly(jsonl_path)
    except Exception:
        return RunSummary(
            run_id=run_id,
            workflow_name="<unknown>",
            status="UNKNOWN",
            ts_start="",
            duration_s=None,
        )

    if not events:
        return RunSummary(
            run_id=run_id,
            workflow_name="<unknown>",
            status="UNKNOWN",
            ts_start="",
            duration_s=None,
        )

    # Extract metadata from WORKFLOW_STARTED event
    workflow_name = "<unknown>"
    ts_start = ""
    ts_end_val: str | None = None

    for ev in events:
        if ev.op == "WORKFLOW_STARTED":
            workflow_name = ev.request.get("function", "<unknown>") or "<unknown>"
            ts_start = ev.ts_start
            if ev.ts_end:
                ts_end_val = ev.ts_end
            break

    status = _classify_run_state(events)

    # Compute duration
    dt_start = _parse_ts(ts_start)
    duration_s: float | None = None
    if dt_start is not None:
        if status in ("FINISHED", "FAILED"):
            # Find a terminal ts_end
            dt_end = _parse_ts(ts_end_val)
            if dt_end is None:
                # Look for last event with ts_end
                for ev in reversed(events):
                    if ev.ts_end:
                        dt_end = _parse_ts(ev.ts_end)
                        break
            if dt_end is not None:
                duration_s = (dt_end - dt_start).total_seconds()
        else:
            now = datetime.now(tz=timezone.utc)
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=timezone.utc)
            duration_s = (now - dt_start).total_seconds()

    return RunSummary(
        run_id=run_id,
        workflow_name=workflow_name,
        status=status,
        ts_start=ts_start,
        duration_s=duration_s,
    )

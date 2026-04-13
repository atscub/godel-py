"""Intervention context bundle for godel repair.

Assembles everything the intervention agent needs: full audit log events,
structured failure info (if the run crashed), a local-state snapshot
reconstructed from step exits, and the source files referenced by the workflow.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from godel._event_log import EventLog
from godel._events import Event, EventStatus

if TYPE_CHECKING:
    pass


@dataclass
class FailureInfo:
    """Structured failure metadata extracted from the audit log."""

    event_id: str
    op: str
    step_path: list[str]
    error: str
    error_type: str
    source_location: str
    remediation_hint: str
    ts_end: str | None


@dataclass
class SourceFile:
    """A workflow source file captured at snapshot time."""

    path: str    # absolute path as recorded in WORKFLOW_STARTED
    content: str  # full file contents
    sha256: str  # so the agent can detect its own edits


@dataclass
class InterventionContext:
    """Everything handed to the intervention agent for a `godel repair` invocation."""

    run_id: str
    run_state: str                     # "PAUSED" | "FAILED" | "RUNNING" | "FINISHED"
    audit_log_path: str                # runs/<run_id>.jsonl
    events: list[Event]
    failure: FailureInfo | None        # None if merely paused or finished
    local_state: dict                  # snapshot reconstructed from audit log
    sources: list[SourceFile]
    workflow_args: dict                # from WORKFLOW_STARTED request
    paused_input_prompt: str | None    # prompt of a blocked @input, if any

    def to_json(self) -> str:
        """Serialize to JSON string for agent consumption."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "run_state": self.run_state,
                "audit_log_path": self.audit_log_path,
                "events": [e.to_dict() for e in self.events],
                "failure": (
                    {
                        "event_id": self.failure.event_id,
                        "op": self.failure.op,
                        "step_path": self.failure.step_path,
                        "error": self.failure.error,
                        "error_type": self.failure.error_type,
                        "source_location": self.failure.source_location,
                        "remediation_hint": self.failure.remediation_hint,
                        "ts_end": self.failure.ts_end,
                    }
                    if self.failure is not None
                    else None
                ),
                "local_state": self.local_state,
                "sources": [
                    {
                        "path": s.path,
                        "content": s.content,
                        "sha256": s.sha256,
                    }
                    for s in self.sources
                ],
                "workflow_args": self.workflow_args,
                "paused_input_prompt": self.paused_input_prompt,
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_run_state(events: list[Event]) -> str:
    """Classify the run state from the event list.

    Rules (evaluated in order):
    - "FINISHED"  — a non-invalidated WORKFLOW_STARTED event has status FINISHED.
    - "FAILED"    — last non-metadata, non-REWIND event has status FAILED.
    - "PAUSED"    — WORKFLOW_STARTED event has status PAUSED, or there is a
                    dangling STARTED input event with no terminal counterpart.
    - "RUNNING"   — none of the above (dangling STARTED without terminator).
    """
    non_invalidated = [e for e in events if e.status != EventStatus.INVALIDATED]

    # Check for terminal WORKFLOW_STARTED
    for ev in non_invalidated:
        if ev.op == "WORKFLOW_STARTED":
            if ev.status == EventStatus.FINISHED:
                return "FINISHED"
            if ev.status == EventStatus.PAUSED:
                return "PAUSED"

    # Check for a dangling PAUSED metadata event
    paused_events = [e for e in non_invalidated if e.op == "PAUSED"]
    if paused_events:
        return "PAUSED"

    # Walk in reverse for last non-REWIND leaf failure
    leaf_ops = {"step.exit", "input", "notify", "prompt"}
    for ev in reversed(non_invalidated):
        if ev.op in leaf_ops or (ev.op not in ("WORKFLOW_STARTED", "PAUSED", "REWIND")):
            if ev.status == EventStatus.FAILED:
                return "FAILED"
            break

    # Dangling STARTED input (paused waiting for user input)
    input_events = [e for e in non_invalidated if e.op == "input"]
    for ev in input_events:
        if ev.status == EventStatus.STARTED:
            return "PAUSED"

    return "RUNNING"


def _extract_failure(events: list[Event]) -> FailureInfo | None:
    """Walk events in reverse and return FailureInfo for the last FAILED non-REWIND event."""
    non_invalidated = [e for e in events if e.status != EventStatus.INVALIDATED]
    for ev in reversed(non_invalidated):
        if ev.op == "REWIND":
            continue
        if ev.status == EventStatus.FAILED:
            resp = ev.response or {}
            return FailureInfo(
                event_id=ev.event_id,
                op=ev.op,
                step_path=list(ev.step_path),
                error=resp.get("error", ""),
                error_type=resp.get("error_type", ""),
                source_location=resp.get("source_location", ""),
                remediation_hint=resp.get("remediation_hint", ""),
                ts_end=ev.ts_end,
            )
    return None


def _snapshot_local_state(events: list[Event]) -> dict:
    """Reconstruct a local state snapshot from step exit FINISHED events."""
    last_step_returns: dict[str, dict] = {}
    for ev in events:
        if ev.op == "step.exit" and ev.status == EventStatus.FINISHED:
            key = "/".join(ev.step_path)
            last_step_returns[key] = ev.response or {}

    recent_ids = [
        e.event_id
        for e in events
        if e.op == "step.exit" and e.status == EventStatus.FINISHED
    ][-5:]

    return {
        "last_step_returns": last_step_returns,
        "recent_step_event_ids": recent_ids,
    }


def _extract_workflow_args(events: list[Event]) -> dict:
    """Return the request dict from the first non-invalidated WORKFLOW_STARTED event."""
    for ev in events:
        if ev.op == "WORKFLOW_STARTED" and ev.status != EventStatus.INVALIDATED:
            return ev.request
    return {}


def _extract_paused_input_prompt(events: list[Event]) -> str | None:
    """Return the prompt text of a dangling STARTED input event, if any."""
    non_invalidated = [e for e in events if e.status != EventStatus.INVALIDATED]
    for ev in reversed(non_invalidated):
        if ev.op == "input" and ev.status == EventStatus.STARTED:
            return ev.request.get("prompt")
    return None


def _read_source_file(path: str) -> SourceFile:
    """Read a source file and compute its sha256."""
    content = Path(path).read_text(encoding="utf-8")
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return SourceFile(path=path, content=content, sha256=sha256)


def _is_safe_source_path(path: str) -> bool:
    """Guard against a tampered audit log exfiltrating arbitrary files.

    A workflow source must be a resolvable regular file with a workflow-source
    extension (``.py`` or ``.gdl``).  This blocks a maliciously crafted log
    pointing at e.g. ``/etc/passwd`` or ``~/.ssh/id_rsa``: such files don't
    carry those extensions and would not be embedded in the bundle.
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file():
        return False
    return resolved.suffix in {".py", ".gdl"}


def _collect_sources(events: list[Event]) -> list[SourceFile]:
    """Collect the top-level workflow source file from WORKFLOW_STARTED."""
    for ev in events:
        if ev.op == "WORKFLOW_STARTED" and ev.status != EventStatus.INVALIDATED:
            source_file = ev.request.get("source_file", "")
            if source_file and _is_safe_source_path(source_file):
                try:
                    return [_read_source_file(source_file)]
                except (OSError, FileNotFoundError):
                    pass
    return []


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_intervention_context(
    run_id: str,
    runs_dir: str = "./runs",
) -> InterventionContext:
    """Build an InterventionContext by loading the audit log for *run_id*.

    Args:
        run_id:   The UUID of the workflow run.
        runs_dir: Directory containing ``<run_id>.jsonl`` log files.

    Returns:
        A fully populated :class:`InterventionContext`.

    Raises:
        FileNotFoundError: if ``<runs_dir>/<run_id>.jsonl`` does not exist.
    """
    log_path = Path(runs_dir) / f"{run_id}.jsonl"
    if not log_path.exists():
        raise FileNotFoundError(
            f"Audit log not found for run {run_id!r}: {log_path}"
        )

    event_log = EventLog.load(run_id, runs_dir=runs_dir)
    event_log.close()
    events = event_log.all_events()

    run_state = _classify_run_state(events)
    failure = _extract_failure(events) if run_state == "FAILED" else None
    local_state = _snapshot_local_state(events)
    workflow_args = _extract_workflow_args(events)
    paused_input_prompt = (
        _extract_paused_input_prompt(events) if run_state == "PAUSED" else None
    )
    sources = _collect_sources(events)

    return InterventionContext(
        run_id=run_id,
        run_state=run_state,
        audit_log_path=str(log_path),
        events=events,
        failure=failure,
        local_state=local_state,
        sources=sources,
        workflow_args=workflow_args,
        paused_input_prompt=paused_input_prompt,
    )

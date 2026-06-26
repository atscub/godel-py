"""ASCII DAG renderer for audit log events."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from godel._events import Event, EventStatus


def _duration(event: Event) -> str:
    if event.ts_end and event.ts_start:
        try:
            t0 = datetime.fromisoformat(event.ts_start)
            t1 = datetime.fromisoformat(event.ts_end)
            dur = (t1 - t0).total_seconds()
            return f"({dur:.1f}s)"
        except ValueError:
            pass
    return ""


STATUS_COLOR: dict[str, str] = {
    "FINISHED": "green",
    "FAILED": "red",
    "STARTED": "yellow",
    "INVALIDATED": "magenta",
    "SUSPENDED": "cyan",
    "PAUSED": "cyan",
}


def _status_icon(status: EventStatus) -> str:
    return {
        EventStatus.FINISHED: "\u2713",
        EventStatus.FAILED: "\u2717",
        EventStatus.STARTED: "\u25cc",
        EventStatus.INVALIDATED: "\u2298",
        EventStatus.SUSPENDED: "\u23f8",
        EventStatus.PAUSED: "\u23f8",
    }.get(status, "?")


def _op_display(event: Event) -> str:
    """Format the op field for display."""
    if event.op == "run" and "cmd" in event.request:
        cmd = event.request["cmd"]
        if isinstance(cmd, list):
            from godel._run import _cmd_display
            cmd = _cmd_display(cmd)
        if len(cmd) > 40:
            cmd = cmd[:37] + "..."
        return f'run "{cmd}"'
    if event.op == "agent.call" and "model" in event.request:
        return f"agent.call ({event.request['model']})"
    return event.op


def _step_key(event: Event) -> tuple:
    """Identity key for grouping retries of the same logical step."""
    return (event.op, tuple(event.step_path), event.invocation_seq)


def _partition_events(events: list[Event]) -> tuple[list[Event], dict[tuple, list[Event]], list[Event]]:
    """Split events into effective, failed-retries, and invalidated.

    Returns:
        effective: events to show in the main view
        retries: {step_key: [failed_events]} for steps that later succeeded
        invalidated: events with INVALIDATED status
    """
    invalidated = [e for e in events if e.status == EventStatus.INVALIDATED]
    non_invalidated = [e for e in events if e.status != EventStatus.INVALIDATED]

    # WORKFLOW_STARTED is structural — a failed one just means a step inside
    # failed and the status cascaded up.  Keep only the last one, silently
    # discard extras (they are never "retries" worth showing).
    wf_events = [e for e in non_invalidated if e.op == "WORKFLOW_STARTED"]
    rest = [e for e in non_invalidated if e.op != "WORKFLOW_STARTED"]
    if wf_events:
        keep_wf = wf_events[-1]
    else:
        keep_wf = None

    # Group the remaining events by step_key to find retries
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in rest:
        groups[_step_key(e)].append(e)

    effective: list[Event] = []
    retries: dict[tuple, list[Event]] = {}

    # Place the single WORKFLOW_STARTED first
    if keep_wf:
        effective.append(keep_wf)

    for key, group in groups.items():
        failed = [e for e in group if e.status == EventStatus.FAILED]
        succeeded = [e for e in group if e.status != EventStatus.FAILED]

        if succeeded and failed:
            # Step was retried: keep the successful one, stash failures
            retries[key] = failed
            effective.extend(succeeded)
        else:
            effective.extend(group)

    return effective, retries, invalidated


# Each line is (text, color_name_or_None, dim)
DagLine = tuple[str, str | None, bool]


def render_dag(events: list[Event], *, show_all: bool = False) -> list[DagLine]:
    """Render event list as ASCII tree lines.

    Returns a list of (text, color, dim) tuples.  The caller decides how
    to paint them (click.style, plain print, etc.).

    When show_all is False (default), FAILED events that were later retried
    successfully and INVALIDATED events are hidden. When True, everything
    is shown with visual grouping.
    """
    if not events:
        return [("(empty run)", None, False)]

    if show_all:
        effective, retries, invalidated = _partition_events(events)
        return _render_all(effective, retries, invalidated)
    else:
        effective, _, _ = _partition_events(events)
        return _render_events(effective)


def render_dag_plain(events: list[Event], *, show_all: bool = False) -> str:
    """Render as a plain string (for tests and non-TTY output)."""
    return "\n".join(text for text, _, _ in render_dag(events, show_all=show_all))


def _event_line(prefix: str, event: Event) -> DagLine:
    """Format a single event as a colored DagLine."""
    icon = _status_icon(event.status)
    dur = _duration(event)
    eid = event.event_id[:8]
    step_str = "/".join(event.step_path) if event.step_path else "(root)"
    op = _op_display(event)
    color = STATUS_COLOR.get(event.status.value)

    if dur:
        text = f"{prefix}\u251c\u2500 [{eid}] {op:<35} {step_str:<25} {dur} {icon}"
    else:
        text = f"{prefix}\u251c\u2500 [{eid}] {op:<35} {step_str:<25} {icon}"
    return (text, color, False)


def _render_events(events: list[Event]) -> list[DagLine]:
    """Render a flat event list as ASCII tree."""
    lines: list[DagLine] = []
    fork_stack: list[Event] = []
    indent = 0
    current_branch: str | None = None

    for event in events:
        eid = event.event_id[:8]
        icon = _status_icon(event.status)
        color = STATUS_COLOR.get(event.status.value)

        if event.op == "FORK":
            prefix = "\u2502  " * indent
            branches = event.request.get("branches", "?")
            lines.append((f"{prefix}\u251c\u2500 [{eid}] FORK ({branches} branches) {icon}", color, False))
            fork_stack.append(event)
            indent += 1
            current_branch = None
            continue

        if event.op == "JOIN":
            indent = max(0, indent - 1)
            if fork_stack:
                fork_stack.pop()
            current_branch = None
            prefix = "\u2502  " * indent
            lines.append((f"{prefix}\u251c\u2500 [{eid}] JOIN {icon}", color, False))
            continue

        # Detect branch boundaries inside a FORK
        if fork_stack and event.step_path:
            branch_name = event.step_path[-1]
            if branch_name != current_branch:
                current_branch = branch_name
                prefix = "\u2502  " * indent
                lines.append((f"{prefix}\u250c\u2500 branch: {branch_name}", None, False))

        # Indent branch content one level deeper than the branch header
        if current_branch is not None:
            prefix = "\u2502  " * (indent + 1)
        else:
            prefix = "\u2502  " * indent

        lines.append(_event_line(prefix, event))

    return lines


def _render_all(effective: list[Event], retries: dict[tuple, list[Event]], invalidated: list[Event]) -> list[DagLine]:
    """Render with full history: retries grouped inline, invalidated at bottom."""
    lines: list[DagLine] = []
    fork_stack: list[Event] = []
    indent = 0
    current_branch: str | None = None

    for event in effective:
        eid = event.event_id[:8]
        icon = _status_icon(event.status)
        color = STATUS_COLOR.get(event.status.value)
        key = _step_key(event)

        if event.op == "FORK":
            prefix = "\u2502  " * indent
            branches = event.request.get("branches", "?")
            lines.append((f"{prefix}\u251c\u2500 [{eid}] FORK ({branches} branches) {icon}", color, False))
            fork_stack.append(event)
            indent += 1
            current_branch = None
            continue

        if event.op == "JOIN":
            indent = max(0, indent - 1)
            if fork_stack:
                fork_stack.pop()
            current_branch = None
            prefix = "\u2502  " * indent
            lines.append((f"{prefix}\u251c\u2500 [{eid}] JOIN {icon}", color, False))
            continue

        # Detect branch boundaries inside a FORK
        if fork_stack and event.step_path:
            branch_name = event.step_path[-1]
            if branch_name != current_branch:
                current_branch = branch_name
                prefix = "\u2502  " * indent
                lines.append((f"{prefix}\u250c\u2500 branch: {branch_name}", None, False))

        # Indent branch content one level deeper than the branch header
        if current_branch is not None:
            prefix = "\u2502  " * (indent + 1)
        else:
            prefix = "\u2502  " * indent

        # Show prior failures grouped above the successful event
        if key in retries:
            failed_list = retries[key]
            n = len(failed_list)
            lines.append((f"{prefix}\u250c\u2500\u2500 \u2717 {n} prior attempt(s):", "red", True))
            for fe in failed_list:
                fe_dur = _duration(fe)
                fe_eid = fe.event_id[:8]
                dur_str = f" {fe_dur}" if fe_dur else ""
                lines.append((f"{prefix}\u2502  [{fe_eid}] {_op_display(fe):<30}{dur_str} \u2717", "red", True))
            lines.append((f"{prefix}\u2514\u2500\u2500 succeeded:", "red", True))

        lines.append(_event_line(prefix, event))

    # Invalidated subgraph at the bottom
    if invalidated:
        lines.append(("", None, False))
        lines.append(("\u2504\u2504\u2504 invalidated (rewind) \u2504\u2504\u2504", "magenta", True))
        for event in invalidated:
            dur = _duration(event)
            eid = event.event_id[:8]
            step_str = "/".join(event.step_path) if event.step_path else "(root)"
            op = _op_display(event)
            if dur:
                lines.append((f"  \u2298 [{eid}] {op:<35} {step_str:<25} {dur}", "magenta", True))
            else:
                lines.append((f"  \u2298 [{eid}] {op:<35} {step_str:<25}", "magenta", True))

    return lines

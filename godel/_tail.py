"""Live tail of a workflow's JSONL audit log.

Provides a pure-async iterator that follows ``./runs/<run_id>.jsonl`` as new
events are appended — the engine for both ``godel tail`` (CLI) and the
programmatic ``godel.tail()`` API.

Design notes
------------
* **Polling, not inotify**: portable across Linux / macOS / WSL.  The default
  100 ms interval keeps CPU overhead negligible for typical workflow step
  durations (which are on the order of seconds).
* **Shared core**: CLI and programmatic callers use the same ``tail()``
  coroutine — intervention agents (awl-qe6) get a first-class async iterator.
* **Rotation / truncation handling**: on each empty read we compare the file's
  current inode and size against the open file handle's position.  If the
  inode changed or the file shrank we reopen.
* **Partial line buffering**: individual lines are buffered until a newline
  arrives so torn writes (theoretically possible despite ``EventLog`` flushing
  after each append) are handled gracefully.
* **Terminal-state detection**: when ``stop_on_terminal=True`` (the default)
  the iterator exits as soon as any WORKFLOW_STARTED event reaches FINISHED or
  FAILED status.
* **Replay-suppression transparency**: during the replay phase of a resumed
  run ``EventLog._replay_suppress`` silences writes, so tail will go quiet
  until the live uncached tail resumes producing.  This is expected and
  documented here so callers understand the silence is transient.
* **Read-only**: this module never opens the JSONL file with ``"a"`` and never
  touches ``_privileged``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator

from godel._events import Event, EventStatus


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_path(run_id: str, runs_dir: Path) -> Path:
    """Resolve *run_id* (or prefix) to an absolute JSONL path.

    Does NOT wait for the file to exist — callers handle that.

    Raises:
        ValueError: if the prefix is ambiguous (multiple matches).
    """
    # Exact match first
    exact = runs_dir / f"{run_id}.jsonl"
    if exact.exists():
        return exact

    # Prefix match
    if runs_dir.exists():
        matches = [f for f in runs_dir.glob("*.jsonl") if f.stem.startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            stems = [f.stem for f in matches]
            raise ValueError(f'Ambiguous prefix "{run_id}" — matches: {stems}')

    # Return the expected path even if it doesn't exist yet — _tail_loop waits
    return exact


async def _wait_for_file(path: Path, poll_interval: float) -> None:
    """Busy-poll until *path* exists (or its parent directory exists if the
    filename is determined but the file hasn't been created yet).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    while not path.exists():
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def tail(
    run_id: str,
    *,
    runs_dir: str | Path = "./runs",
    follow: bool = True,
    poll_interval: float = 0.1,
    stop_on_terminal: bool = True,
) -> AsyncIterator[Event]:
    """Yield :class:`~godel._events.Event` objects as they are appended to
    ``<runs_dir>/<run_id>.jsonl``.

    Parameters
    ----------
    run_id:
        Full run ID or unique prefix.  Prefix resolution happens once at
        startup; if the file doesn't exist yet the iterator waits for it to
        appear.
    runs_dir:
        Directory containing JSONL run logs (default: ``"./runs"``).
    follow:
        If ``True`` (default) keep tailing after EOF and yield new events as
        they arrive.  If ``False`` read to current EOF and stop.
    poll_interval:
        Seconds between empty-read polls (default: 0.1 s).
    stop_on_terminal:
        If ``True`` (default) stop the iterator when a WORKFLOW_STARTED event
        transitions to FINISHED or FAILED.  Set to ``False`` to observe across
        resume invocations.

    Yields
    ------
    Event
        Deserialized events in append order.

    Raises
    ------
    ValueError
        If *run_id* is an ambiguous prefix matching more than one log file.
    """
    runs_path = Path(runs_dir)
    path = _resolve_path(run_id, runs_path)

    await _wait_for_file(path, poll_interval)

    buf = ""
    fh = open(path)  # noqa: SIM115  (async-safe: non-blocking reads in poll loop)
    last_ino = os.fstat(fh.fileno()).st_ino

    try:
        while True:
            chunk = fh.read(65536)
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = Event.from_dict(d)
                    yield event

                    if stop_on_terminal and _is_terminal(event):
                        return
            else:
                # Empty read — EOF
                if not follow:
                    return

                # Check for rotation / truncation
                try:
                    st = path.stat()
                except FileNotFoundError:
                    # File deleted — wait for it to reappear
                    await _wait_for_file(path, poll_interval)
                    fh.close()
                    fh = open(path)
                    last_ino = os.fstat(fh.fileno()).st_ino
                    buf = ""
                    continue

                current_ino = st.st_ino
                current_size = st.st_size
                tell = fh.tell()

                if current_ino != last_ino or current_size < tell:
                    # Rotation or truncation — reopen
                    fh.close()
                    fh = open(path)
                    last_ino = os.fstat(fh.fileno()).st_ino
                    buf = ""
                    continue

                await asyncio.sleep(poll_interval)
    finally:
        fh.close()


def _is_terminal(event: Event) -> bool:
    """Return True if *event* indicates a terminal workflow state."""
    return (
        event.op == "WORKFLOW_STARTED"
        and event.status in (EventStatus.FINISHED, EventStatus.FAILED)
    )

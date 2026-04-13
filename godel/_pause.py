"""Pause-request sentinel file helpers.

A pause request is signalled by the presence of a JSON file at
``./runs/<run_id>.pause``.  The file contains::

    {"reason": "...", "requested_ts": "..."}

Absent file means no pause requested.  ``check_pause_request`` is called at
the top of every live @step execution; it raises ``PauseSignal`` so the
enclosing @workflow can emit a PAUSED event and exit cleanly.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from godel._exceptions import PauseSignal

# run_id must be a safe filename component: alphanumerics, dash, underscore only.
# Rejects path separators, `..`, and any character that could escape the runs_dir.
_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9_\-]{1,128}\Z")


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(
            f"invalid run_id {run_id!r}: must match {_RUN_ID_RE.pattern}"
        )


def _pause_path(run_id: str, runs_dir: str = "./runs") -> Path:
    _validate_run_id(run_id)
    return Path(runs_dir) / f"{run_id}.pause"


def check_pause_request(run_id: str, runs_dir: str = "./runs") -> None:
    """Raise ``PauseSignal`` if a pause sentinel file exists for *run_id*.

    This is a no-op when the file is absent.  Designed to be called at the
    top of the ``@step`` wrapper on every live (non-replay) step so that a
    pause request is honoured at the next replayable boundary.
    """
    path = _pause_path(run_id, runs_dir)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # Corrupt / unreadable file — treat as no pause pending
        return
    raise PauseSignal(
        reason=payload.get("reason", ""),
        request_ts=payload.get("requested_ts", ""),
    )


def write_pause_request(
    run_id: str,
    reason: str = "",
    runs_dir: str = "./runs",
) -> Path:
    """Write a pause sentinel file for *run_id*.

    Creates ``./runs/<run_id>.pause`` with ``{reason, requested_ts}``
    using an atomic write: content is written to a unique temporary file
    in the same directory, then ``os.replace`` renames it to the final
    path.  If the process crashes between the write and the rename, the
    temporary file is cleaned up on the next call to
    ``clear_pause_request``.

    Returns the path for callers that need to inspect or clean it up.
    """
    import os

    path = _pause_path(run_id, runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "requested_ts": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(payload)
    # Write to a unique temp file in the same directory so that os.replace
    # is guaranteed to be atomic on POSIX (same filesystem).
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, suffix=f".{run_id}.pause.tmp")
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.replace(tmp_str, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def clear_pause_request(run_id: str, runs_dir: str = "./runs") -> None:
    """Remove the pause sentinel file for *run_id* (idempotent).

    Also removes any ``*.<run_id>.pause.tmp`` orphan files left in the same
    directory in case a previous call to ``write_pause_request`` crashed
    between the temp-file write and the atomic rename.  The glob is scoped to
    *run_id* so that concurrent runs are not affected.
    """
    path = _pause_path(run_id, runs_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    # Clean up any orphaned temp files that belong to *this* run_id.  These are
    # created by write_pause_request with a suffix of ".<run_id>.pause.tmp".
    # Scoping the glob to the run_id prevents accidentally removing live temp
    # files belonging to other concurrent runs.
    runs_path = path.parent
    if runs_path.is_dir():
        for orphan in runs_path.glob(f"*.{run_id}.pause.tmp"):
            orphan.unlink(missing_ok=True)


def pause(run_id: str, *, reason: str = "", runs_dir: str = "./runs") -> str:
    """Request a running workflow pause at its next @step boundary.

    Resolves *run_id* as a prefix against ``./runs/<run_id>.jsonl`` files,
    writes the pause sentinel, and returns the resolved full run_id.

    Raises:
        FileNotFoundError: if no matching run exists (or the runs/ directory
            is absent).
        ValueError: if *run_id* is an ambiguous prefix that matches more than
            one run.
    """
    _validate_run_id(run_id)
    runs_path = Path(runs_dir)
    if not runs_path.exists():
        raise FileNotFoundError("No runs/ directory found")
    matches = [f for f in runs_path.glob("*.jsonl") if f.stem.startswith(run_id)]
    if not matches:
        raise FileNotFoundError(f'No run matching "{run_id}"')
    if len(matches) > 1:
        names = [f.stem for f in matches]
        raise ValueError(f'Ambiguous prefix "{run_id}" — matches: {names}')
    full = matches[0].stem
    write_pause_request(full, reason, runs_dir=runs_dir)
    return full

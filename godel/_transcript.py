"""TranscriptWriter — JSONL writer for advisory observability events.

File layout (inside a run directory):
  transcript.jsonl        — current (active) file
  transcript.jsonl.1      — most-recently rotated-out file
  transcript.jsonl.2      — older, etc.

Line 1 of every file is a shape-distinct header:
  {"header": {"v": 1, "run_id": "...", "started_at": "<iso8601>"}}

The "header" top-level key is intentionally distinct from the "event" key used
for every other line, so line-1 detection requires only a key-presence check.

Every subsequent line is an event:
  {"event": {"ts": "<iso>", "seq": <int>, "op": "...", "step_path": [...],
              "stream_path": [...], ...}}

seq is strictly monotonic starting at 1 and never resets across rotations.

Rotation is triggered when (current file size + encoded line) >= max_bytes:
  1. A sentinel event  {"event": {"op": "rotate", "last_seq": N,
                        "prev": "transcript.jsonl.<k+1>", ...}} is appended
     as the LAST line of the outgoing file (current → .k).
     "last_seq" is the seq of the last real event written to this file.
     "prev" points to the next-older file in the chain.
     IMPORTANT: sentinel events carry NO "seq" field.  Including self._seq
     (which has already been pre-incremented for the triggering event) would
     collide with the first real event written to the next file.  Readers
     must treat the absence of "seq" on a rotate-op event as intentional;
     use "last_seq" instead to locate the file boundary.
  2. flush() + os.fsync() ensures the sentinel is durable.
  3. Existing .N files are shifted to .N+1; current is renamed to .1.
  4. A fresh transcript.jsonl is opened with a new header (same run_id, same
     seq counter — the sequence is NOT reset).

NOTE: Crash recovery (reopening a pre-existing transcript.jsonl from a prior
crashed run) is NOT supported in v1.  Callers must use a fresh run_dir per
run; reusing a run_dir after a crash will produce duplicate seq numbers.

Reader contract (authoritative — mirror in user-facing docs when transcript
reader docs ship; see godel-py-5pl.14 / follow-up ticket):
  * Line 1 of every file: {"header": {...}} — never has an "event" key and
    never carries a "seq" field.
  * Every other line: {"event": {...}} — real events have a "seq" field
    (strictly monotonic across the entire run, never reset across rotations).
  * Rotation sentinels are events with "op": "rotate".  They have NO "seq"
    field.  Use "last_seq" for the file-boundary anchor.  Readers that treat
    every event row as having a "seq" MUST first filter out rotate-op rows.
  * "last_seq" on a sentinel is always >= 1 — the writer will not rotate a
    file that has no real events (avoids misleading last_seq=0 sentinels).

Schema versioning: v follows semver on the major component.  A reader may raise
TranscriptVersionError if the major version it finds exceeds the highest major
it understands.  Unknown minor versions are silently accepted.

NOTE on stream_path vs stream_id: The live-observability-v3 plan spec used a
scalar "stream_id" field.  The ticket (godel-py-5pl.1) supersedes the plan and
requires a list-typed "stream_path" for hierarchical stream addressing (e.g.
["agent", "claude", "tool_call"]).  Downstream reader tickets are authored
against the "stream_path" shape.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from godel._redact import RedactorRegistry


TRANSCRIPT_FORMAT_VERSION = 1
_FILENAME = "transcript.jsonl"
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TranscriptVersionError(Exception):
    """Raised when a transcript header carries an unsupported major version."""


class TranscriptWriter:
    """Thread-safe, rotation-aware JSONL transcript writer.

    Parameters
    ----------
    run_dir:
        Directory that holds the transcript files (typically ``runs/<run_id>/``).
        Created if it does not exist.  NOTE: Do NOT reuse a run_dir from a
        prior crashed run — crash recovery is out of scope for v1 and will
        produce duplicate seq numbers.
    max_bytes:
        Soft upper bound on a single file.  Checked before each write; if
        ``current_size + encoded_line >= max_bytes`` rotation fires first.
        Defaults to 50 MB; override via ``GODEL_TRANSCRIPT_MAX_BYTES`` env var.
        Explicit ``max_bytes`` kwarg takes precedence over the env var.
    run_id:
        Embedded in every header for cross-file correlation.  Defaults to the
        basename of *run_dir*.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        max_bytes: int | None = None,
        run_id: str | None = None,
        redactors: list | None = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)

        env_max = os.environ.get("GODEL_TRANSCRIPT_MAX_BYTES")
        if max_bytes is not None:
            self._max_bytes = max_bytes
        elif env_max is not None:
            self._max_bytes = int(env_max)
        else:
            self._max_bytes = _DEFAULT_MAX_BYTES

        self._run_id: str = run_id or self._run_dir.name
        self._redactor_registry = RedactorRegistry(redactors)
        self._lock = threading.Lock()
        # _seq is the most-recently ASSIGNED seq number.
        # Invariant: every event that has been durably written to disk has
        # seq <= self._seq.  Incremented BEFORE encoding; if _rotate() raises
        # after the increment but before _write_line(), we decrement back.
        self._seq = 0
        # seq of the most-recently WRITTEN real event (not sentinel).
        # Used to populate sentinel.last_seq accurately.
        self._last_written_seq = 0
        self._file: IO[str] | None = None
        self._file_size = 0
        # Whether the currently-open file has at least one real (non-sentinel)
        # event written to it.  Guards against rotating a header-only file,
        # which would emit a sentinel with last_seq=0 (misleading).  Reset in
        # _open_fresh(), set in write_event() after _write_line() succeeds.
        self._current_file_has_real_event = False

        self._open_fresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_event(
        self,
        op: str,
        step_path: list[str] | tuple[str, ...] | None = None,
        stream_path: list[str] | tuple[str, ...] | None = None,
        **extra: object,
    ) -> int | None:
        """Append one event line and return its seq number.

        Rotation (if needed) happens transparently before the event is written.

        Parameters
        ----------
        op:
            Operation name, e.g. ``"step_start"``, ``"step_end"``.
        step_path:
            Hierarchical step address, e.g. ``["fetch", "parse"]``.
        stream_path:
            Hierarchical stream address (e.g. ``["agent", "claude"]``).
            Defaults to ``[]``.
        **extra:
            Additional op-specific fields merged into the event dict.

        Returns
        -------
        int | None
            The seq of the written event, or ``None`` if a redactor dropped the
            event (no line was written, no seq was consumed).

        Raises
        ------
        RuntimeError
            If called after ``close()``.
        """
        with self._lock:
            if self._file is None:
                raise RuntimeError(
                    "TranscriptWriter is closed; cannot write_event after close()"
                )
            self._seq += 1
            seq = self._seq
            event_body: dict[str, object] = {
                "ts": _now_iso(),
                "seq": seq,
                "op": op,
                "step_path": list(step_path) if step_path else [],
                "stream_path": list(stream_path) if stream_path else [],
            }
            event_body.update(extra)
            serialised_body = _encode(event_body)

            # Apply redaction pipeline (string-based: operates on the serialised
            # event body, before the outer {"event": ...} wrapper is added).
            # sentinel_extras carries the standard event fields so that when a
            # redactor raises, the written sentinel is a well-formed event line.
            sentinel_extras = {
                "ts": event_body["ts"],
                "seq": seq,
                "step_path": event_body["step_path"],
                "stream_path": event_body["stream_path"],
            }
            redacted = self._redactor_registry.apply(
                serialised_body, sentinel_extras=sentinel_extras
            )
            if redacted is None:
                # A redactor dropped the event — roll back seq (the next real
                # write will reuse this number to keep seq contiguous) and
                # return None so callers can unambiguously distinguish a dropped
                # event from a written one.  Returning the pre-rollback seq
                # would collide with the next real event's seq, misleading any
                # caller that tracks "last written seq" via the return value.
                self._seq -= 1
                return None

            # redacted is either the (possibly transformed) original body or a
            # sentinel JSON string injected by the error-substitution path.
            # Wrap in the standard {"event": ...} envelope.
            line = '{"event":' + redacted + "}"

            try:
                self._maybe_rotate(line)
            except Exception:
                # Rotation failed: seq was incremented but event not written.
                # Roll back so the next successful write gets a contiguous seq.
                self._seq -= 1
                raise
            self._write_line(line)
            self._last_written_seq = seq
            self._current_file_has_real_event = True
            return seq

    def close(self) -> None:
        """Flush and close the current transcript file."""
        with self._lock:
            self._flush_and_sync()
            if self._file and not self._file.closed:
                self._file.close()
            self._file = None

    def __enter__(self) -> "TranscriptWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers (must be called with self._lock held)
    # ------------------------------------------------------------------

    def _open_fresh(self) -> None:
        """Open (or re-open after rotation) transcript.jsonl and write the header.

        Opens in append mode.  If the file already exists and is non-empty
        (e.g. from a prior crashed run), the header is NOT re-written and
        self._file_size is set to the current on-disk size.  seq is NOT
        restored — crash recovery is out of scope for v1.
        """
        path = self._run_dir / _FILENAME
        self._file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: WPS515
        self._file_size = path.stat().st_size
        self._current_file_has_real_event = False
        if self._file_size == 0:
            self._write_header()

    def _write_header(self) -> None:
        header = {
            "header": {
                "v": TRANSCRIPT_FORMAT_VERSION,
                "run_id": self._run_id,
                "started_at": _now_iso(),
            }
        }
        line = _encode(header)
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        """Write *line* + newline to the open file, updating the size counter."""
        if self._file is None:
            raise RuntimeError("TranscriptWriter is closed")
        encoded_len = len(line.encode("utf-8")) + 1  # +1 for the newline byte
        self._file.write(line + "\n")
        self._file_size += encoded_len

    def _flush_and_sync(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass  # e.g. already closed or unsupported on this OS/FS

    def _maybe_rotate(self, upcoming_line: str) -> None:
        """Rotate if writing *upcoming_line* would push the file over max_bytes.

        Guard: do NOT rotate a file that has no real events yet.  Rotating an
        empty file (header only) would produce a sentinel with last_seq=0,
        which is misleading — it suggests a file boundary with no preceding
        data.  This case is reachable with pathologically small max_bytes or
        pathologically large single events.  In that case we accept that this
        one event will push the file over the soft bound; the next write will
        rotate normally.
        """
        encoded_size = len(upcoming_line.encode("utf-8")) + 1
        if self._file_size + encoded_size < self._max_bytes:
            return
        # Find last real event written to the CURRENT file by checking whether
        # any real event has been written since the most recent _open_fresh().
        # We track this via _current_file_has_real_event (set in write_event
        # after a successful append).
        if not self._current_file_has_real_event:
            return
        self._rotate()

    def _rotate(self) -> None:
        """Execute a single rotation step.

        Rotation protocol:
          1. Determine how many suffixed files already exist (highest = n-1).
             The current file will become .1; existing .k → .(k+1).
          2. Write sentinel as the LAST line of the outgoing file.
             sentinel has NO "seq" field (avoids collision with the first real
             event seq in the new file — see module docstring for rationale).
             sentinel.last_seq = seq of the last real event in this file.
             sentinel.prev     = the name the next-older file will have AFTER
                                 the rename cascade (i.e. "transcript.jsonl.2"
                                 when the current .1 shifts to .2, etc.).
          3. flush() + fsync().
          4. Rename cascade: .N→.(N+1), ..., .1→.2.
          5. Rename current → .1.
          6. Open a fresh transcript.jsonl with a new header.

        If any step after writing the sentinel fails (e.g. os.rename raises),
        the writer is left in an unusable state and the exception propagates to
        write_event, which rolls back self._seq.
        """
        base = self._run_dir / _FILENAME

        # Count how many suffixed files already exist.
        # After rotation, current becomes .1; existing .1 becomes .2; etc.
        # So if the highest existing suffix is (n-1), the old .1 will become .2,
        # meaning the sentinel in the outgoing (current → .1) file should point
        # to .2 as its predecessor if .1 currently exists.
        n = 1
        while (self._run_dir / f"{_FILENAME}.{n}").exists():
            n += 1
        # n is now one past the highest existing suffix (minimum: n=1 means no
        # suffixed files yet).  After renaming, old .1 → .2, ..., old .(n-1) →
        # .n.  The file we are writing sentinel into becomes .1.  Its older
        # neighbour (if any) will be at .2 after the cascade.
        prev_name = f"{_FILENAME}.{2}" if n > 1 else None

        # Write sentinel as LAST line.
        # NOTE: sentinels intentionally carry NO "seq" field.  self._seq has
        # already been pre-incremented by write_event() for the triggering event
        # and will be assigned to the first real event in the new file — so
        # including it here would create a seq collision visible to any reader
        # that iterates all lines including sentinels.  "last_seq" is the sole
        # authoritative seq reference on a sentinel (= seq of the last real
        # event durably written to this file).  Readers MUST NOT expect a "seq"
        # key on rotate-op events; its absence is part of the reader contract.
        sentinel_body: dict[str, object] = {
            "ts": _now_iso(),
            "last_seq": self._last_written_seq,  # accurate: last durable event
            "op": "rotate",
            "step_path": [],
            "stream_path": [],
            "prev": prev_name,
        }
        sentinel_line = _encode({"event": sentinel_body})
        self._write_line(sentinel_line)

        # Flush + fsync before renaming.
        self._flush_and_sync()
        self._file.close()  # type: ignore[union-attr]
        self._file = None

        # Shift existing suffixed files: .(n-1) → .n, ..., .1 → .2
        for i in range(n - 1, 0, -1):
            src = self._run_dir / f"{_FILENAME}.{i}"
            dst = self._run_dir / f"{_FILENAME}.{i + 1}"
            os.rename(src, dst)

        # Move current → .1
        os.rename(base, self._run_dir / f"{_FILENAME}.1")

        # Open a fresh transcript.jsonl with a header.
        self._open_fresh()

    # ------------------------------------------------------------------
    # Reader-side helper (class method, usable without a writer instance)
    # ------------------------------------------------------------------

    @staticmethod
    def check_version(header: dict[str, object]) -> None:
        """Raise TranscriptVersionError if *header* carries an unsupported major version.

        Accepts any v <= TRANSCRIPT_FORMAT_VERSION and silently accepts
        unknown minor versions (semver contract: major-only gating).

        Parameters
        ----------
        header:
            The parsed dict from the ``"header"`` key of line 1.

        Raises
        ------
        TranscriptVersionError
            If the major version found in *header* exceeds
            ``TRANSCRIPT_FORMAT_VERSION``.
        """
        v = header.get("v", 1)
        if v > TRANSCRIPT_FORMAT_VERSION:
            raise TranscriptVersionError(
                f"Transcript format version {v} is not supported by this version of "
                f"godel (highest understood major: {TRANSCRIPT_FORMAT_VERSION}). "
                f"Upgrade godel to read this transcript."
            )


# ---------------------------------------------------------------------------
# Module-level encode helper (shared by TranscriptWriter and tests)
# ---------------------------------------------------------------------------

def _encode(obj: dict[str, object]) -> str:
    return json.dumps(obj, separators=(",", ":"))

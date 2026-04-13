"""TranscriptWriter — JSONL writer for advisory observability events.

File layout (inside a run directory):
  transcript.jsonl        — current (active) file
  transcript.jsonl.1      — most-recently rotated-out file
  transcript.jsonl.2      — older, etc.

Line 1 of every file is a shape-distinct header:
  {"header": {"v": 1, "run_id": "...", "started_at": "<iso8601>"}}

Every subsequent line is an event:
  {"event": {"ts": "<iso>", "seq": <int>, "op": "...", "step_path": [...],
              "stream_path": [...], ...}}

seq is strictly monotonic starting at 1 and never resets across rotations.

Rotation is triggered when (current file size + encoded line) >= max_bytes:
  1. A sentinel event  {"event": {..., "op": "rotate", "prev": "transcript.jsonl.1"}}
     is appended as the LAST line of the outgoing file.
  2. flush() + os.fsync() ensures the sentinel is durable.
  3. Existing .N files are shifted to .N+1; current is renamed to .1.
  4. A fresh transcript.jsonl is opened with a new header (same run_id, same seq
     counter — the sequence is NOT reset).

Schema versioning: v follows semver on the major component.  A reader may raise
TranscriptVersionError if the major version it finds exceeds the highest major it
understands.  Unknown minor versions are silently accepted.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


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
        Created if it does not exist.
    max_bytes:
        Soft upper bound on a single file.  Checked before each write; if
        ``current_size + encoded_line >= max_bytes`` rotation fires first.
        Defaults to 50 MB; override via ``GODEL_TRANSCRIPT_MAX_BYTES`` env var.
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
        self._lock = threading.Lock()
        self._seq = 0  # next seq to assign; pre-increment makes first event seq=1
        self._file: "os.IO[str]" | None = None  # type: ignore[type-arg]
        self._file_size = 0

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
    ) -> int:
        """Append one event line and return its seq number.

        This is the primary write method.  Rotation (if needed) happens
        transparently before the event is written.

        Parameters
        ----------
        op:
            Operation name, e.g. ``"step_start"``, ``"step_end"``.
        step_path:
            Hierarchical step address, e.g. ``["fetch", "parse"]``.
        stream_path:
            Stream address (for streaming ops).  Defaults to ``[]``.
        **extra:
            Additional fields merged into the event dict.
        """
        with self._lock:
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
            line = self._encode({"event": event_body})
            self._maybe_rotate(line)
            self._write_line(line)
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
        """Open (or re-open after rotation) transcript.jsonl and write the header."""
        path = self._run_dir / _FILENAME
        # Use line-buffered text mode so partial-line writes are minimised.
        self._file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: WPS515
        # On first open the file is empty; after rotation it is freshly created.
        self._file_size = path.stat().st_size
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
        line = self._encode(header)
        self._write_line(line)

    @staticmethod
    def _encode(obj: dict) -> str:  # type: ignore[type-arg]
        return json.dumps(obj, separators=(",", ":"))

    def _write_line(self, line: str) -> None:
        """Write *line* + newline to the open file, updating the size counter."""
        assert self._file is not None
        encoded = (line + "\n").encode("utf-8")
        self._file.write(line + "\n")
        self._file_size += len(encoded)

    def _flush_and_sync(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass  # e.g. already closed or unsupported

    def _maybe_rotate(self, upcoming_line: str) -> None:
        """Rotate if writing *upcoming_line* would exceed max_bytes."""
        encoded_size = len((upcoming_line + "\n").encode("utf-8"))
        if self._file_size + encoded_size < self._max_bytes:
            return
        self._rotate()

    def _rotate(self) -> None:
        """Execute a single rotation step."""
        base = self._run_dir / _FILENAME

        # 1. Determine highest existing suffix so we can shift them up.
        n = 1
        while (self._run_dir / f"{_FILENAME}.{n}").exists():
            n += 1
        # n is now one past the highest existing suffix (or 1 if none exist).

        # 2. Write sentinel as the LAST line of the outgoing file.
        sentinel_body: dict[str, object] = {
            "ts": _now_iso(),
            "seq": self._seq,  # same seq as last real event (informational)
            "op": "rotate",
            "step_path": [],
            "stream_path": [],
            "prev": f"{_FILENAME}.1",
        }
        sentinel_line = self._encode({"event": sentinel_body})
        self._write_line(sentinel_line)

        # 3. Flush + fsync before renaming.
        self._flush_and_sync()
        self._file.close()  # type: ignore[union-attr]
        self._file = None

        # 4. Shift existing suffixed files: .N → .(N+1), ..., .1 → .2
        for i in range(n - 1, 0, -1):
            src = self._run_dir / f"{_FILENAME}.{i}"
            dst = self._run_dir / f"{_FILENAME}.{i + 1}"
            os.rename(src, dst)

        # 5. Move current → .1
        os.rename(base, self._run_dir / f"{_FILENAME}.1")

        # 6. Open a fresh file with a new header.
        self._open_fresh()

    # ------------------------------------------------------------------
    # Reader-side helper (class method, usable without a writer instance)
    # ------------------------------------------------------------------

    @staticmethod
    def check_version(header: dict) -> None:  # type: ignore[type-arg]
        """Raise TranscriptVersionError if *header* carries an unsupported major version.

        Parameters
        ----------
        header:
            The parsed dict from the ``"header"`` key of line 1.
        """
        v = header.get("v", 1)
        if v > TRANSCRIPT_FORMAT_VERSION:
            raise TranscriptVersionError(
                f"Transcript format version {v} is not supported by this version of "
                f"godel (highest understood major: {TRANSCRIPT_FORMAT_VERSION}). "
                f"Upgrade godel to read this transcript."
            )

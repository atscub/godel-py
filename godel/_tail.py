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

``TranscriptTail`` — rotation-chain-aware tail reader
------------------------------------------------------
A synchronous iterator over transcript events produced by
:class:`~godel._transcript.TranscriptWriter`.  Unlike the async ``tail()``
coroutine above (which follows a single ``runs/<run_id>.jsonl`` audit log),
``TranscriptTail`` understands the multi-file rotation chain:

* ``transcript.jsonl``        — active (live) file
* ``transcript.jsonl.1``      — most-recently rotated-out
* ``transcript.jsonl.2``      — older, etc.

Key behaviours:

* **Inode tracking**: the reader tracks the inode of the open file handle.  On
  each empty read it compares path inode vs handle inode.  If they differ
  *and* no ``rotate`` sentinel was seen, a crash recovery path is taken.
* **Sentinel-driven rotation**: when the ``rotate`` op is parsed, the reader
  closes the current fd, waits for ``transcript.jsonl`` to reappear (the
  writer renames current → .1 then creates a fresh current), records the new
  inode, and resumes reading.
* **Late-attach via** ``from_run(run_id, runs_dir)``: discovers ``.N`` archive
  files via glob, sorts from oldest to newest, replays them in full, then
  attaches to the live file.
* **Missing current file**: if ``transcript.jsonl`` does not exist after one
  poll interval, :class:`TranscriptTailError` is raised (typed error, does not
  hang).
* **Inode reuse recovery**: if the inode changes without a sentinel (writer
  crash scenario), the reader logs a warning once and reopens.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator, Generator

from godel._events import Event, EventStatus

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# TranscriptTail — rotation-chain-aware tail reader (ticket godel-py-5pl.2)
# ---------------------------------------------------------------------------

_TRANSCRIPT_FILENAME = "transcript.jsonl"


class TranscriptTailError(Exception):
    """Raised when the transcript reader encounters an unrecoverable error.

    Attributes
    ----------
    path:
        The filesystem path that triggered the error (if applicable).
    """

    def __init__(self, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


class TranscriptTail:
    """Synchronous iterator over transcript events from a rotation chain.

    Reads ``<run_dir>/transcript.jsonl`` (and archives ``.1``, ``.2``, …) as
    produced by :class:`~godel._transcript.TranscriptWriter`.

    Parameters
    ----------
    run_dir:
        Directory containing ``transcript.jsonl`` and its rotated archives.
    poll_interval:
        Seconds to sleep between empty-read polls.  Default: 0.1 s.
    follow:
        If ``True`` (default) keep tailing after reaching the live EOF.
        If ``False`` stop at the first EOF in the current file (useful for
        batch replay of already-closed transcripts).
    _start_files:
        Internal: list of archive paths to replay *before* attaching to the
        live file.  Used by ``from_run()``.  Do not pass directly.

    Raises
    ------
    TranscriptTailError
        If the live ``transcript.jsonl`` does not appear within one poll
        interval and ``follow=True``, or immediately if ``follow=False``.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        poll_interval: float = 0.1,
        follow: bool = True,
        _start_files: list[Path] | None = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._poll_interval = poll_interval
        self._follow = follow
        self._start_files: list[Path] = _start_files or []

    # ------------------------------------------------------------------
    # Class-method constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_run(
        cls,
        run_id: str,
        runs_dir: str | Path = "./runs",
        *,
        poll_interval: float = 0.1,
        follow: bool = True,
    ) -> "TranscriptTail":
        """Construct a late-attach reader for *run_id*.

        Discovers archived ``.N`` files under ``<runs_dir>/<run_id>/``,
        sorts them from oldest to newest, and schedules their replay before
        attaching to the live ``transcript.jsonl``.

        Parameters
        ----------
        run_id:
            The workflow run identifier; also the directory name under
            *runs_dir*.
        runs_dir:
            Parent directory of per-run directories.  Default: ``"./runs"``.
        poll_interval:
            Forwarded to the constructor.
        follow:
            Forwarded to the constructor.
        """
        run_dir = Path(runs_dir) / run_id
        # Discover .N archive files: transcript.jsonl.1, .2, …
        archives: list[tuple[int, Path]] = []
        i = 1
        while True:
            p = run_dir / f"{_TRANSCRIPT_FILENAME}.{i}"
            if p.exists():
                archives.append((i, p))
                i += 1
            else:
                break
        # Sort from oldest (highest N) to newest (.1); replay in write order.
        archives.sort(key=lambda t: t[0], reverse=True)
        start_files = [p for _, p in archives]
        return cls(
            run_dir,
            poll_interval=poll_interval,
            follow=follow,
            _start_files=start_files,
        )

    # ------------------------------------------------------------------
    # Iterator protocol
    # ------------------------------------------------------------------

    def __iter__(self) -> Generator[dict, None, None]:
        """Yield raw transcript event dicts in write order.

        Each yielded dict is the *inner* event body (i.e. the value of the
        ``"event"`` key in the JSONL line), with the header lines skipped.
        Callers that need the rotate sentinel itself will find it as a dict
        with ``op == "rotate"``.
        """
        yield from self._iter_files()

    def events(self) -> Generator[dict, None, None]:
        """Alias for ``__iter__``; yields raw transcript event dicts."""
        yield from self._iter_files()

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _parse_lines(self, buf: str, new_chunk: str) -> tuple[list[dict], str]:
        """Parse complete newline-terminated lines from *buf* + *new_chunk*.

        Returns
        -------
        (events, remaining_buf)
            *events* is a (possibly empty) list of parsed dicts with an
            ``"event"`` key (header lines are silently skipped).
            *remaining_buf* is any trailing partial line.
        """
        buf += new_chunk
        events: list[dict] = []
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in obj:
                events.append(obj["event"])
            # Header lines (key "header") are silently skipped.
        return events, buf

    def _read_archive(self, path: Path) -> Generator[dict, None, None]:
        """Yield all events from an archive file (does not follow).

        Archive files are complete (closed) — no polling needed.  If the
        file disappears before or during open (renamed by the writer during
        a concurrent rotation cascade), this method returns without yielding
        any events.  The caller's gap-fill loop should rescan.
        """
        buf = ""
        try:
            fh = open(path, encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            for raw_line in fh:
                evts, buf = self._parse_lines(buf, raw_line)
                yield from evts
            # Flush any remaining buffer (should be empty for well-formed files)
            if buf.strip():
                try:
                    obj = json.loads(buf.strip())
                    if "event" in obj:
                        yield obj["event"]
                except json.JSONDecodeError:
                    pass
        finally:
            fh.close()

    def _initial_wait_for_current(self) -> Path:
        """Wait for ``transcript.jsonl`` to appear at startup.

        * ``follow=False``: raises immediately if absent (no polling).
        * ``follow=True``: polls indefinitely until the file appears.

        This is called only once at startup (Phase 2 of ``_iter_files``).  A
        file that disappears *after* the reader has been running triggers
        ``TranscriptTailError`` via the crash-detection path in
        ``_iter_files``, not here.

        Returns
        -------
        Path
            The path to ``transcript.jsonl`` once it exists.

        Raises
        ------
        TranscriptTailError
            If ``follow=False`` and the file is absent.
        """
        current = self._run_dir / _TRANSCRIPT_FILENAME
        if current.exists():
            return current
        if not self._follow:
            raise TranscriptTailError(
                f"Transcript file not found: {current}", path=current
            )
        # Poll indefinitely (follow=True) — writer hasn't started yet.
        while not current.exists():
            time.sleep(self._poll_interval)
        return current

    def _wait_for_fresh_current(self, max_wait: float = 2.0) -> Path:
        """Wait for ``transcript.jsonl`` to exist (any inode).

        Called after gap-filling when we need to open the live file.  Unlike
        ``_post_rotate_wait_for_current``, this does NOT require a specific
        inode — we just need the file to be present so we can open it.

        Raises
        ------
        TranscriptTailError
            If no file appears within *max_wait* seconds.
        """
        current = self._run_dir / _TRANSCRIPT_FILENAME
        deadline = time.monotonic() + max_wait
        while not current.exists():
            if time.monotonic() >= deadline:
                raise TranscriptTailError(
                    f"transcript.jsonl not found after gap-fill (waited {max_wait}s): "
                    f"{current}",
                    path=current,
                )
            time.sleep(min(self._poll_interval, 0.005))
        return current

    def _post_rotate_wait_for_current(
        self, sentinel_ino: int, max_wait: float = 2.0
    ) -> Path:
        """Wait for a *fresh* ``transcript.jsonl`` after a rotate sentinel.

        After the writer appends a sentinel and renames current → .1, it
        immediately creates a new ``transcript.jsonl``.  We detect the "new"
        file by waiting until the inode at the path differs from the inode
        of the file that contained the sentinel (*sentinel_ino*).

        This correctly handles the window between "sentinel written to old
        path" and "old path renamed away + new path created": we keep
        polling until the inode changes (or a new file appears with a
        different inode).

        Parameters
        ----------
        sentinel_ino:
            Inode of the file that contained the rotate sentinel.  We wait
            until the path's inode differs from this value.
        max_wait:
            Maximum seconds to wait.  Default: 2 s.

        Raises
        ------
        TranscriptTailError
            If the new file does not appear within *max_wait* seconds.
        """
        current = self._run_dir / _TRANSCRIPT_FILENAME
        deadline = time.monotonic() + max_wait
        while True:
            try:
                st = current.stat()
                if st.st_ino != sentinel_ino:
                    return current
            except FileNotFoundError:
                pass  # file not yet created; keep polling
            if time.monotonic() >= deadline:
                raise TranscriptTailError(
                    f"Fresh transcript.jsonl (different inode) not found after "
                    f"rotate (waited {max_wait}s): {current}",
                    path=current,
                )
            time.sleep(min(self._poll_interval, 0.005))

    def _fill_gaps(
        self,
        last_emitted_seq_ref: list[int],
        seen_inodes: set[int] | None = None,
    ) -> Generator[dict, None, None]:
        """Yield events from all archive files with seqs > *last_emitted_seq_ref[0]*.

        Collects events from all available archives in a convergence loop,
        sorts them by seq, and emits in order.  This avoids the race where a
        newer archive (lower suffix, higher seqs) is processed before an older
        archive (higher suffix, lower seqs) has appeared — which would advance
        ``last_emitted_seq_ref`` past the older archive's events, causing them
        to be permanently filtered.

        Parameters
        ----------
        last_emitted_seq_ref:
            A one-element list containing the mutable highest emitted seq.
            Updated as events are yielded so the caller's state stays in sync.
        seen_inodes:
            Shared set of archive inodes already fully read across previous
            ``_fill_gaps`` calls.  Pass a single set to every call so that
            inode-dedup persists across Phase-1b and post-rotation fills.
            If ``None`` a fresh set is used (single-call mode).
        """
        if seen_inodes is None:
            seen_inodes = set()

        # Convergence loop: keep scanning for new archives until none appear
        # for max_empty_retries consecutive iterations.
        max_iters = 200  # safety valve against infinite loops
        empty_streak = 0
        max_empty_retries = 8

        # Accumulate all events across all iterations before emitting.
        # This prevents ordering races where a newer archive (lower .N suffix,
        # higher seqs) is processed before an older archive (higher suffix,
        # lower seqs) exists yet.
        pending_real: list[tuple[int, dict]] = []  # (seq, evt)
        pending_rotate: list[dict] = []  # sentinel/rotate events

        for _ in range(max_iters):
            newly_opened = self._open_all_archives(seen_inodes)
            if not newly_opened:
                empty_streak += 1
                if empty_streak >= max_empty_retries:
                    break
                time.sleep(0.005)
                continue
            empty_streak = 0
            for ino, fh in newly_opened:
                try:
                    had_events = False
                    for evt in self._read_from_fh(fh):
                        if evt.get("op") == "rotate":
                            pending_rotate.append(evt)
                            had_events = True
                        else:
                            seq = evt.get("seq")
                            if isinstance(seq, int):
                                pending_real.append((seq, evt))
                                had_events = True
                    if had_events:
                        seen_inodes.add(ino)
                finally:
                    try:
                        fh.close()
                    except Exception:
                        pass
        else:
            logger.warning(
                "TranscriptTail._fill_gaps: convergence not reached after %d "
                "iterations; some events may be missing.",
                max_iters,
            )

        # Emit sentinels first (informational; order among them doesn't matter).
        yield from pending_rotate

        # Emit real events sorted by seq, deduplicating against last_emitted_seq_ref.
        pending_real.sort(key=lambda t: t[0])
        for seq, evt in pending_real:
            if seq > last_emitted_seq_ref[0]:
                yield evt
                last_emitted_seq_ref[0] = seq

    def _open_all_archives(
        self, skip_inodes: set[int]
    ) -> list[tuple[int, object]]:
        """Open all ``.N`` archive files not in *skip_inodes*.

        Discovers files via glob (robust to rename-cascade numbering gaps),
        opens each file immediately so subsequent renames cannot swap content,
        filters by inode to avoid re-reading already-processed files, and
        sorts by path suffix DESCENDING so older files (higher ``N`` = rotated
        earlier) are emitted before newer ones.

        Using the suffix for ordering is more robust than reading the first
        event's seq: a newly-created archive may have only a header line (no
        real events yet), causing ``_first_seq`` to return ``INT_MAX`` and the
        file to be processed last — after ``last_emitted_seq`` has advanced
        past its events, causing those events to be permanently filtered.

        Returns
        -------
        list of (inode, fh)
            New file handles sorted by suffix descending (oldest events first).
            Caller is responsible for closing each handle.
        """
        # Discover all .N files via glob, recording each file's numeric suffix.
        candidates: list[tuple[int, Path]] = []  # (suffix_int, path)
        for p in self._run_dir.glob(f"{_TRANSCRIPT_FILENAME}.*"):
            suffix = p.name[len(_TRANSCRIPT_FILENAME) + 1:]
            if suffix.isdigit():
                candidates.append((int(suffix), p))

        # Sort by suffix descending: higher suffix = older file = lower first_seq.
        # We want to process oldest archives first so last_emitted_seq_ref
        # advances monotonically and the dedup filter does not hide later events.
        candidates.sort(key=lambda t: t[0], reverse=True)

        # Open each file immediately (in sorted order) and record its inode.
        opened: list[tuple[int, object]] = []  # (inode, fh)
        for _suffix, path in candidates:
            try:
                fh = open(path, encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                ino = os.fstat(fh.fileno()).st_ino
            except OSError:
                fh.close()
                continue
            if ino in skip_inodes:
                fh.close()
                continue
            opened.append((ino, fh))

        return opened

    def _read_from_fh(self, fh: object) -> Generator[dict, None, None]:
        """Yield all events from an already-open file handle (does not follow)."""
        buf = ""
        try:
            for raw_line in fh:  # type: ignore[union-attr]
                evts, buf = self._parse_lines(buf, raw_line)
                yield from evts
            if buf.strip():
                try:
                    obj = json.loads(buf.strip())
                    if "event" in obj:
                        yield obj["event"]
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    def _find_unread_archives(self, after_seq: int) -> list[Path]:
        """Return archive paths that contain events with seq > *after_seq*.

        .. deprecated::
            Use :meth:`_open_unread_archives` instead, which opens files
            immediately to avoid rename-cascade content swaps.  This method
            is retained for compatibility with :meth:`_read_archive`.
        """
        candidates: list[tuple[int, Path]] = []
        for p in self._run_dir.glob(f"{_TRANSCRIPT_FILENAME}.*"):
            suffix = p.name[len(_TRANSCRIPT_FILENAME) + 1:]
            if suffix.isdigit():
                candidates.append((int(suffix), p))
        if not candidates:
            return []
        candidates.sort(key=lambda t: t[0], reverse=True)
        unread: list[Path] = []
        for _, path in candidates:
            max_seq = self._peek_max_seq(path)
            if max_seq is not None and max_seq > after_seq:
                unread.append(path)
        return unread

    def _peek_max_seq(self, path: Path) -> int | None:
        """Return the highest real-event seq in *path*, or None if unreadable.

        Reads the file and scans for the highest ``seq`` among non-sentinel
        events.  This is an O(file-size) operation but archives are typically
        small (capped at max_bytes from the writer).
        """
        max_seq: int | None = None
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "event" in obj:
                        evt = obj["event"]
                        if evt.get("op") != "rotate":
                            seq = evt.get("seq")
                            if isinstance(seq, int) and (max_seq is None or seq > max_seq):
                                max_seq = seq
        except OSError:
            pass
        return max_seq

    def _iter_files(self) -> Generator[dict, None, None]:
        """Core generator: replay archives then tail the live file."""
        # Track the highest seq we have emitted so we can detect gaps after
        # rotation.  -1 means we haven't yielded any real events yet.
        last_emitted_seq: int = -1

        # Phase 1: replay pre-existing archive files in write order.
        # _start_files is populated by from_run(); for direct construction
        # it is empty, but we still scan for archives below (Phase 1b).
        for archive_path in self._start_files:
            for evt in self._read_archive(archive_path):
                yield evt
                if evt.get("op") != "rotate":
                    seq = evt.get("seq")
                    if isinstance(seq, int):
                        last_emitted_seq = seq

        # Phase 2: tail the live transcript.jsonl by inode.
        current = self._initial_wait_for_current()

        fd = os.open(str(current), os.O_RDONLY)
        fh = os.fdopen(fd, "r", encoding="utf-8")
        last_ino = os.fstat(fd).st_ino
        buf = ""
        inode_warning_issued = False

        # Use a mutable container so _fill_gaps can update last_emitted_seq.
        seq_ref = [last_emitted_seq]

        # Shared inode set across all _fill_gaps calls for this reader session.
        # Prevents re-reading archives already processed, and ensures the inode
        # dedup is consistent across Phase-1b and all post-rotation fills.
        archive_seen_inodes: set[int] = set()

        # Phase 1b (for direct constructor without _start_files): after opening
        # the live file, fill any archive gaps that appeared before we attached.
        # This handles the case where the writer already rotated one or more files
        # before the reader opened transcript.jsonl.  We do this AFTER opening
        # the live fd so that _post_rotate_wait_for_current's inode check works;
        # seq-dedup in the live-read loop prevents duplicate emission.
        if not self._start_files:
            # Phase 1b: fill any archive gaps that appeared before we attached.
            # We do NOT pre-add last_ino to archive_seen_inodes: if the live
            # file gets rotated during this fill, _fill_gaps should read it so
            # we don't miss its events.  The seq-dedup filter in the live-read
            # loop prevents duplicate emission.
            yield from self._fill_gaps(seq_ref, archive_seen_inodes)
        last_emitted_seq = seq_ref[0]

        # Counter for periodic archive re-scan at EOF.  Even after Phase-1b,
        # there may be archives created in the race window between Phase-1b's
        # convergence and the live-file open.  We re-scan every
        # _PERIODIC_FILL_POLLS polls to catch these stragglers.
        _PERIODIC_FILL_POLLS = 5
        _eof_poll_count = 0

        try:
            while True:
                chunk = fh.read(65536)
                if chunk:
                    inode_warning_issued = False  # reset on successful read
                    evts, buf = self._parse_lines(buf, chunk)
                    rotate_seen = False
                    for evt in evts:
                        if evt.get("op") == "rotate":
                            # Always yield sentinel (informational) even if we
                            # already processed events up to this seq via archives.
                            yield evt
                            rotate_seen = True
                            break  # sentinel is always last; stop this batch
                        seq = evt.get("seq")
                        if isinstance(seq, int) and seq <= last_emitted_seq:
                            continue  # already emitted from archive replay; skip
                        # Gap detection: if seq > last_emitted_seq + 1, there
                        # are intermediate events in archives not yet read.
                        # Fill those gaps before emitting this event.
                        if (
                            isinstance(seq, int)
                            and last_emitted_seq >= 0
                            and seq > last_emitted_seq + 1
                        ):
                            seq_ref[0] = last_emitted_seq
                            yield from self._fill_gaps(seq_ref, archive_seen_inodes)
                            last_emitted_seq = seq_ref[0]
                            # Re-check if this event is now a duplicate.
                            if seq <= last_emitted_seq:
                                continue
                        yield evt
                        if isinstance(seq, int):
                            last_emitted_seq = seq
                    if rotate_seen:
                        # Sentinel seen: writer has renamed current → .1 and
                        # created a fresh transcript.jsonl.
                        sentinel_ino = last_ino  # inode of the file with the sentinel
                        fh.close()
                        buf = ""
                        inode_warning_issued = False
                        if not self._follow:
                            # Non-follow mode: stop after reading the complete
                            # chain up to the sentinel.
                            return
                        # Wait for the fresh current file (different inode) to appear.
                        # We do this FIRST so we know when the rename cascade is done.
                        current = self._post_rotate_wait_for_current(sentinel_ino)
                        # The sentinel file (old live file, now .1) was already
                        # read via the live fd — mark its inode so _fill_gaps
                        # won't re-process it and emit duplicates.
                        archive_seen_inodes.add(sentinel_ino)
                        # Fill gaps: any archives written while we slept.
                        seq_ref[0] = last_emitted_seq
                        yield from self._fill_gaps(seq_ref, archive_seen_inodes)
                        last_emitted_seq = seq_ref[0]
                        # Open the fresh current file.  Retry if the file
                        # disappears between _wait_for_fresh_current and os.open
                        # (possible if the writer rotates again during fill_gaps).
                        while True:
                            current = self._wait_for_fresh_current()
                            try:
                                fd = os.open(str(current), os.O_RDONLY)
                            except FileNotFoundError:
                                time.sleep(min(self._poll_interval, 0.005))
                                continue
                            break
                        fh = os.fdopen(fd, "r", encoding="utf-8")
                        last_ino = os.fstat(fd).st_ino
                        # Add new live fd's inode to seen_inodes so periodic fills
                        # don't re-process it if it becomes an archive.
                        archive_seen_inodes.add(last_ino)
                else:
                    # Empty read — EOF.
                    if not self._follow:
                        return

                    # Check for inode change without sentinel (crash recovery).
                    # Race window: the writer may write the sentinel and rename
                    # current → .1 between our last chunk-read and this stat.
                    # Strategy:
                    #   1. Re-read the old fd before giving up — if the sentinel
                    #      was written after our last read, we'll find it now and
                    #      handle it via the normal sentinel path (avoids false
                    #      TranscriptTailError and avoids seq-reset).
                    #   2. If the old fd still shows EOF, stat the path.
                    #      If the file is gone briefly (mid-rename), wait up to
                    #      poll_interval for it to reappear.
                    #   3. If the file reappears with a new inode (rotation
                    #      without sentinel seen yet), treat as sentinel-less
                    #      rotation: fill gaps then reopen.
                    #   4. If the file is still gone after the grace period,
                    #      raise TranscriptTailError (writer crash).

                    # Step 1: re-read the old fd in case sentinel just arrived.
                    late_chunk = fh.read(65536)
                    if late_chunk:
                        # New data appeared — process it in the main loop.
                        buf += late_chunk
                        continue

                    # Step 2: stat the path.
                    # Use a generous grace window (at least 2× poll_interval,
                    # minimum 100 ms) to survive the window between the writer's
                    # atomic rename(current → .1) and its subsequent open() of
                    # the fresh file, which can take several milliseconds on a
                    # loaded system or slow filesystem.
                    grace = max(self._poll_interval * 2, 0.1)
                    deadline_grace = time.monotonic() + grace
                    st = None
                    while True:
                        try:
                            st = current.stat()
                            break
                        except FileNotFoundError:
                            if time.monotonic() >= deadline_grace:
                                raise TranscriptTailError(
                                    f"Transcript file disappeared: {current}",
                                    path=current,
                                )
                            time.sleep(0.002)

                    path_ino = st.st_ino
                    if path_ino != last_ino:
                        # Step 3: inode changed — likely a rotation whose sentinel
                        # we missed (race).  Re-read the old fd one more time to
                        # catch a sentinel that arrived just after our last read.
                        late_chunk2 = fh.read(65536)
                        if late_chunk2:
                            buf += late_chunk2
                            continue  # let the main loop parse the sentinel
                        # Sentinel not in old fd.  Log once, fill gaps, reopen.
                        if not inode_warning_issued:
                            logger.warning(
                                "TranscriptTail: inode changed for %s without rotate "
                                "sentinel (expected %d, got %d). Reopening — some "
                                "events may have been lost.",
                                current,
                                last_ino,
                                path_ino,
                            )
                            inode_warning_issued = True
                        fh.close()
                        buf = ""
                        # Fill any gaps from archives that accumulated before
                        # the crash/swap (old writer's events in .N files).
                        seq_ref[0] = last_emitted_seq
                        pre_fill_seq = last_emitted_seq
                        yield from self._fill_gaps(seq_ref, archive_seen_inodes)
                        last_emitted_seq = seq_ref[0]
                        # Open the current file with retry (TOCTOU: it may
                        # disappear between _wait_for_fresh_current and os.open).
                        while True:
                            current = self._wait_for_fresh_current()
                            try:
                                fd = os.open(str(current), os.O_RDONLY)
                            except FileNotFoundError:
                                time.sleep(min(self._poll_interval, 0.005))
                                continue
                            break
                        fh = os.fdopen(fd, "r", encoding="utf-8")
                        last_ino = os.fstat(fd).st_ino
                        # Decide whether to reset seq tracking:
                        # * If _fill_gaps found new archive events (seq advanced),
                        #   the inode change was due to a rotation whose sentinel
                        #   was missed.  Keep last_emitted_seq so subsequent events
                        #   are properly deduped and gap-detected.
                        # * If _fill_gaps found nothing (seq unchanged), this looks
                        #   like a writer crash / inode reuse: the new file likely
                        #   has a fresh writer starting at seq 1.  Reset so those
                        #   events are not silently filtered.
                        if last_emitted_seq == pre_fill_seq:
                            last_emitted_seq = -1
                            seq_ref[0] = -1
                        continue

                    # Periodic archive re-scan: catch archives that were missed
                    # by Phase-1b due to timing races (writer still rotating
                    # when Phase-1b converged).  Only run every N polls to keep
                    # overhead low during normal tailing.
                    _eof_poll_count += 1
                    if _eof_poll_count >= _PERIODIC_FILL_POLLS:
                        _eof_poll_count = 0
                        seq_ref[0] = last_emitted_seq
                        yield from self._fill_gaps(seq_ref, archive_seen_inodes)
                        last_emitted_seq = seq_ref[0]

                    time.sleep(self._poll_interval)
        finally:
            try:
                fh.close()
            except Exception:
                pass

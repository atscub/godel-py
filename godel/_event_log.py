"""EventLog — append-only JSONL audit log for workflow events."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

from godel._events import Event, EventStatus
from godel._context import _privileged


class EventLog:
    """In-memory event DAG with append-only JSONL persistence."""

    def __init__(self, run_id: str, runs_dir: str):
        self._run_id = run_id
        self._events: list[Event] = []
        self._events_by_id: dict[str, Event] = {}
        self._seq_counter = 0
        self._replay_suppress = False
        self._file_path = Path(runs_dir) / f"{run_id}.jsonl"
        token = _privileged.set(True)
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._file_path, "a")
        finally:
            _privileged.reset(token)

    def emit_started(
        self,
        op: str,
        step_path: tuple[str, ...],
        request: dict,
        *,
        invocation_seq: int = 0,
        step_local_seq: int = 0,
        parent_event_id: str | None = None,
        stream_path: list[str] | None = None,
    ) -> Event:
        """Create and persist a STARTED event.

        If *parent_event_id* is given, the new event is appended to
        the parent's ``children_ids`` and the parent is re-persisted
        (last-snapshot-wins).

        *stream_path* is the list of launch-site ULIDs computed at subprocess /
        agent launch time on the calling thread and captured in the reader-thread
        closure.  Callers that do not participate in subprocess nesting may omit
        it (defaults to ``[]``).
        """
        event_id = str(ULID())
        request_hash = Event.compute_request_hash(request)
        event = Event(
            event_id=event_id,
            run_id=self._run_id,
            seq=self._seq_counter,
            step_path=step_path,
            invocation_seq=invocation_seq,
            step_local_seq=step_local_seq,
            op=op,
            request_hash=request_hash,
            request=request,
            status=EventStatus.STARTED,
            ts_start=datetime.now(timezone.utc).isoformat(),
            stream_path=list(stream_path) if stream_path is not None else [],
        )
        self._seq_counter += 1
        self._events.append(event)
        self._events_by_id[event_id] = event

        # Link parent → child
        if parent_event_id:
            parent = self._events_by_id.get(parent_event_id)
            if parent:
                parent.children_ids.append(event_id)
                self._append_event(parent)  # re-persist with updated children

        self._append_event(event)
        return event

    def emit_finished(
        self,
        event_id: str,
        response: dict,
        *,
        status: EventStatus = EventStatus.FINISHED,
    ) -> Event:
        """Update event to FINISHED (or given) status and persist."""
        event = self._events_by_id[event_id]
        event.status = status
        event.response = response
        event.ts_end = datetime.now(timezone.utc).isoformat()
        self._append_event(event)
        return event

    def emit_failed(
        self,
        event_id: str,
        error: str,
        response: dict | None = None,
        *,
        error_type: str = "",
        step_path: tuple | list = (),
        source_location: str = "",
        remediation_hint: str = "",
    ) -> Event:
        """Update event to FAILED status with structured error metadata and persist.

        If *response* is supplied directly, it is used as-is and all keyword
        arguments must be at their defaults — mixing both is a caller error.
        Otherwise a response dict is built from the keyword arguments.
        All keyword arguments have defaults so existing ``emit_failed(id, str(exc))``
        call sites continue to work without modification.
        """
        if response is not None:
            # Guard: callers must not silently discard keyword params by passing response=
            assert not error_type and not step_path and not source_location and not remediation_hint, (
                "emit_failed: keyword params (error_type, step_path, source_location, "
                "remediation_hint) are ignored when response= is provided. "
                "Build the response dict yourself or omit response=."
            )
        event = self._events_by_id[event_id]
        event.status = EventStatus.FAILED
        if response is not None:
            event.response = response
        else:
            event.response = {
                "error": error,
                "error_type": error_type or "Exception",
                "step_path": list(step_path),
                "source_location": source_location,
                "remediation_hint": remediation_hint,
            }
        event.ts_end = datetime.now(timezone.utc).isoformat()
        self._append_event(event)
        return event

    def emit_suspended(self, event_id: str) -> Event:
        """Transition event to SUSPENDED status, clear children_ids, and persist."""
        event = self._events_by_id[event_id]
        event.status = EventStatus.SUSPENDED
        event.children_ids = []
        event.ts_end = datetime.now(timezone.utc).isoformat()
        self._append_event(event)
        return event

    def get_event(self, event_id: str) -> Event | None:
        """Look up event by ID."""
        return self._events_by_id.get(event_id)

    def all_events(self) -> list[Event]:
        """Return all events in append order."""
        return list(self._events)

    def _append_event(self, event: Event) -> None:
        """Write event snapshot to JSONL file.

        When ``_replay_suppress`` is True (replay phase of a resumed run),
        writes are silently skipped so the log is not polluted with duplicates.
        """
        if self._replay_suppress:
            return
        token = _privileged.set(True)
        try:
            line = json.dumps(event.to_dict(), separators=(",", ":"))
            self._file.write(line + "\n")
            self._file.flush()
        finally:
            _privileged.reset(token)

    @classmethod
    def load(cls, run_id: str, runs_dir: str) -> EventLog:
        """Reconstruct EventLog from JSONL file. Last snapshot per event_id wins."""
        log = cls.__new__(cls)
        log._run_id = run_id
        log._events = []
        log._events_by_id = {}
        log._seq_counter = 0
        log._replay_suppress = False
        file_path = Path(runs_dir) / f"{run_id}.jsonl"
        log._file_path = file_path

        # Read all lines, last snapshot per event_id wins
        raw_events: dict[str, Event] = {}
        ordered_ids: list[str] = []
        max_seq = -1

        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                event = Event.from_dict(d)
                if event.event_id not in raw_events:
                    ordered_ids.append(event.event_id)
                raw_events[event.event_id] = event
                if event.seq > max_seq:
                    max_seq = event.seq

        # Rebuild in first-seen order
        for eid in ordered_ids:
            event = raw_events[eid]
            log._events.append(event)
            log._events_by_id[eid] = event

        log._seq_counter = max_seq + 1 if max_seq >= 0 else 0
        token = _privileged.set(True)
        try:
            log._file = open(file_path, "a")
        finally:
            _privileged.reset(token)
        return log

    def get_full_payload(
        self, event_id: str, runs_dir: str | None = None
    ) -> dict:
        """Retrieve untruncated request prompt and full response for *event_id*.

        Reads ``agent.prompt`` and ``agent.response`` events from the run's
        ``transcript.jsonl`` rotation chain, matching by the event's
        ``stream_path``.

        Parameters
        ----------
        event_id:
            ID of the audit log event whose full payload is requested.
        runs_dir:
            Parent directory of per-run directories.  If ``None``, derived
            from ``self._file_path.parent``.

        Returns
        -------
        dict with keys:
            ``event_id``, ``op``, ``step_path``, ``stream_path``,
            ``request`` (full prompt string or ``None``),
            ``response`` (assembled response string or ``None``),
            ``model`` (from transcript if available).

        Raises
        ------
        KeyError
            If *event_id* is not found in the loaded log.
        FileNotFoundError
            If the transcript.jsonl does not exist for this run.
        """
        from pathlib import Path
        from godel._tail import TranscriptTail, TranscriptTailError

        event = self._events_by_id.get(event_id)
        if event is None:
            raise KeyError(f"Event not found: {event_id}")

        run_id = event.run_id
        target_stream_path = list(event.stream_path)

        # Locate transcript directory
        if runs_dir is None:
            transcript_dir = self._file_path.parent / run_id
        else:
            transcript_dir = Path(runs_dir) / run_id

        transcript_file = transcript_dir / "transcript.jsonl"
        if not transcript_file.exists():
            raise FileNotFoundError(
                f"No transcript found for run {run_id!r}: {transcript_file}"
            )

        full_prompt: str | None = None
        response_chunks: list[str] = []
        model: str | None = None

        reader = TranscriptTail.from_run(run_id, runs_dir=transcript_dir.parent, follow=False)
        try:
            for evt in reader:
                evt_stream = evt.get("stream_path", [])
                if evt_stream != target_stream_path:
                    continue
                op = evt.get("op")
                if op == "agent.prompt":
                    full_prompt = evt.get("prompt")
                    if model is None and evt.get("model"):
                        model = evt["model"]
                elif op == "agent.response":
                    text = evt.get("text")
                    if text:
                        response_chunks.append(text)
                    if model is None and evt.get("model"):
                        model = evt["model"]
        except TranscriptTailError as exc:
            raise FileNotFoundError(
                f"Transcript read error for run {run_id!r}: {exc}"
            ) from exc

        full_response: str | None = "".join(response_chunks) if response_chunks else None

        return {
            "event_id": event_id,
            "op": event.op,
            "step_path": list(event.step_path),
            "stream_path": target_stream_path,
            "model": model,
            "request": full_prompt,
            "response": full_response,
        }

    def close(self) -> None:
        """Flush and close the JSONL file."""
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()

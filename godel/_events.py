"""Event dataclass and EventStatus enum for audit logging."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from ulid import ULID


class EventStatus(Enum):
    STARTED = "STARTED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"
    SUSPENDED = "SUSPENDED"
    PAUSED = "PAUSED"


@dataclass
class Event:
    event_id: str
    run_id: str
    seq: int
    children_ids: list[str] = field(default_factory=list)
    step_path: tuple[str, ...] = ()
    invocation_seq: int = 0
    step_local_seq: int = 0
    op: str = ""
    request_hash: str = ""
    request: dict = field(default_factory=dict)
    response: dict | None = None
    status: EventStatus = EventStatus.STARTED
    ts_start: str = ""
    ts_end: str | None = None
    # stream_path is stamped at subprocess/agent launch time on the launching
    # thread and captured in the reader-thread closure.  It is a list of short
    # ULIDs representing the nesting chain of launches: [] for top-level events,
    # [launch_id] for direct subprocess launches, [parent_id, child_id] for
    # nested subprocess launches (e.g., agent shells out to git), etc.
    stream_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for JSONL (convert enum to string, tuple to list)."""
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "children_ids": self.children_ids,
            "step_path": list(self.step_path),
            "invocation_seq": self.invocation_seq,
            "step_local_seq": self.step_local_seq,
            "op": self.op,
            "request_hash": self.request_hash,
            "request": self.request,
            "response": self.response,
            "status": self.status.value,
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "stream_path": list(self.stream_path),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Event:
        """Deserialize from JSONL dict."""
        return cls(
            event_id=d["event_id"],
            run_id=d["run_id"],
            seq=d["seq"],
            children_ids=d.get("children_ids", []),
            step_path=tuple(d.get("step_path", ())),
            invocation_seq=d.get("invocation_seq", 0),
            step_local_seq=d.get("step_local_seq", 0),
            op=d.get("op", ""),
            request_hash=d.get("request_hash", ""),
            request=d.get("request", {}),
            response=d.get("response"),
            status=EventStatus(d["status"]),
            ts_start=d.get("ts_start", ""),
            ts_end=d.get("ts_end"),
            stream_path=d.get("stream_path", []),
        )

    # Keys excluded from request_hash computation.
    # source_hash records which version of the function body was executed but
    # must NOT participate in the content-hash used for replay matching —
    # editing source should trigger the source-edit guardrail, not a
    # request_hash mismatch.  Keeping them separate lets each policy (
    # --on-mismatch vs --on-source-edit) operate independently.
    # auto_checkpoint records how scripted-stdin answers were supplied
    # (env-var mode, e.g. "pipe"/"file"/"fifo"/"1").  It's execution-context
    # metadata, not part of the workflow's logical request identity, so a
    # resume that changes or drops the mode must not trigger a hash mismatch.
    # assumed_idempotent_source and idempotent are replay-safety metadata, not
    # semantic inputs — they must not participate in request_hash, or (a) a
    # STARTED-only entry retroactively promoted on resume would fail to match
    # itself on a second resume (C4), and (b) retroactively adding
    # run(..., idempotent=True) would trip --on-mismatch=abort (C5).
    _HASH_EXCLUDE_KEYS: frozenset[str] = frozenset({
        "source_hash", "auto_checkpoint", "assumed_idempotent_source", "idempotent",
        "session_id",  # ctor-supplied session_id must not affect replay hash matching
        "replay",  # read_text replay mode is a resume-behavior hint, not a semantic input
    })

    @staticmethod
    def compute_request_hash(request: dict) -> str:
        """SHA-256 of canonical JSON, excluding non-participating keys."""
        filtered = {k: v for k, v in request.items() if k not in Event._HASH_EXCLUDE_KEYS}
        canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

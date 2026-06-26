"""Intervention toolset — rewind, resume, input, give_up, read_file, edit_file.

Exposes the intervention toolbox to the repair agent.  Each tool is callable
from the agent loop; terminal-signal exceptions (ResumeRequested, GaveUp) are
caught by the default intervention workflow and the ``godel repair`` CLI.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from godel._event_log import EventLog
from godel._events import EventStatus
from godel._rewind import apply_rewind


# ---------------------------------------------------------------------------
# Terminal-signal exceptions
# ---------------------------------------------------------------------------


class _InterventionDone(Exception):
    """Base class for terminal control-flow signals from the intervention loop."""

    def __init__(self, outcome: str, reason: str = ""):
        self.outcome = outcome
        self.reason = reason
        super().__init__(f"{outcome}: {reason}" if reason else outcome)


class ResumeRequested(_InterventionDone):
    """Raised by InterventionToolset.resume().

    Caught by the default intervention workflow and ``godel repair`` CLI,
    which then spawns ``godel resume <run_id>`` after the intervention
    workflow returns cleanly.
    """

    def __init__(self, reason: str = ""):
        super().__init__("resume", reason)


class GaveUp(_InterventionDone):
    """Raised by InterventionToolset.give_up().

    Indicates the intervention agent could not repair the workflow.
    An UNRECOVERABLE metadata event is written to the audit log before
    this exception is raised.
    """

    def __init__(self, reason: str = ""):
        super().__init__("give_up", reason)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class RewindArgs(BaseModel):
    to: list[str]
    reason: str = ""


class RewindResult(BaseModel):
    invalidated_count: int
    invalidated_ids: list[str]
    already_rewound_ids: list[str] = Field(default_factory=list)


class ResumeArgs(BaseModel):
    reason: str = ""


class InputArgs(BaseModel):
    value: str


class GiveUpArgs(BaseModel):
    reason: str


class ReadFileArgs(BaseModel):
    path: str


class ReadFileResult(BaseModel):
    path: str
    content: str
    sha256: str


class EditFileArgs(BaseModel):
    path: str
    old_str: str
    new_str: str
    expected_sha256: str | None = None


class EditFileResult(BaseModel):
    path: str
    new_sha256: str
    edits_applied: int


# ---------------------------------------------------------------------------
# Toolset class
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InterventionToolset:
    """Agent-facing toolset bound to a specific intervention session.

    Args:
        ctx:      The :class:`~godel.intervention.InterventionContext` for the
                  run being repaired.
        runs_dir: Directory containing ``<run_id>.jsonl`` files.
    """

    def __init__(self, ctx: Any, runs_dir: str = "./runs"):
        self._ctx = ctx
        self._runs_dir = runs_dir

    # ------------------------------------------------------------------
    # rewind
    # ------------------------------------------------------------------

    async def rewind(self, args: RewindArgs) -> RewindResult:
        """Invalidate completed events and prepare for re-execution.

        Delegates to :func:`godel._rewind.apply_rewind`.  Safety checks are
        enforced: if the subtree to be invalidated contains a non-idempotent
        ``run()`` call, :class:`~godel._exceptions.RewindUnsafe` propagates
        to the caller (treated as a tool error by the agent loop).

        Args:
            args: :class:`RewindArgs` with target event ID(s) and optional reason.

        Returns:
            :class:`RewindResult` with invalidated_count and invalidated_ids.

        Raises:
            RewindUnsafe: If the subtree contains non-idempotent run() events.
            ValueError: If a target event_id does not exist.
        """
        event_log = EventLog.load(self._ctx.run_id, runs_dir=self._runs_dir)
        try:
            result = apply_rewind(event_log, args.to, reason=args.reason)
        finally:
            event_log.close()
        return RewindResult(
            invalidated_count=result["invalidated_count"],
            invalidated_ids=result["invalidated_ids"],
            already_rewound_ids=result["already_rewound_ids"],
        )

    # ------------------------------------------------------------------
    # resume
    # ------------------------------------------------------------------

    async def resume(self, args: ResumeArgs) -> None:
        """Signal that the workflow should resume.

        Does NOT spawn a subprocess — raises :class:`ResumeRequested` which is
        caught by the caller (default intervention workflow or ``godel repair``
        CLI), which then spawns ``godel resume <run_id>``.

        Raises:
            ResumeRequested: Always.
        """
        raise ResumeRequested(args.reason)

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------

    async def input(self, args: InputArgs) -> None:
        """Inject a value into the paused ``input()`` call.

        Finds the trailing STARTED ``input`` event in the audit log and emits a
        FINISHED snapshot with the supplied value.  On subsequent resume,
        :class:`~godel._replay.ReplayWalker` returns the injected value from
        cache without blocking on stdin.

        Args:
            args: :class:`InputArgs` with the value to inject.

        Raises:
            ValueError: If no STARTED input event is found in the audit log.
        """
        event_log = EventLog.load(self._ctx.run_id, runs_dir=self._runs_dir)
        try:
            # Find the last STARTED input event (the one the workflow is paused at).
            paused_input = None
            for ev in reversed(event_log.all_events()):
                if ev.op == "input" and ev.status == EventStatus.STARTED:
                    paused_input = ev
                    break

            if paused_input is None:
                raise ValueError(
                    f"No paused input event found for run {self._ctx.run_id!r}. "
                    "The workflow must be blocked at an input() call to inject a value."
                )

            event_log.emit_finished(
                paused_input.event_id,
                response={"value": args.value},
            )
        finally:
            event_log.close()

    # ------------------------------------------------------------------
    # give_up
    # ------------------------------------------------------------------

    async def give_up(self, args: GiveUpArgs) -> None:
        """Declare the intervention failed and mark the run as unrecoverable.

        Writes an append-only ``UNRECOVERABLE`` metadata event to the audit log,
        then raises :class:`GaveUp`.

        Args:
            args: :class:`GiveUpArgs` with a mandatory reason.

        Raises:
            GaveUp: Always (after the metadata event is persisted).
        """
        event_log = EventLog.load(self._ctx.run_id, runs_dir=self._runs_dir)
        try:
            unrecoverable_event = event_log.emit_started(
                op="UNRECOVERABLE",
                step_path=(),
                request={"reason": args.reason},
                invocation_seq=-1,
                step_local_seq=-1,
            )
            event_log.emit_finished(
                unrecoverable_event.event_id,
                response={"reason": args.reason},
            )
        finally:
            event_log.close()

        raise GaveUp(args.reason)

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    async def read_file(self, args: ReadFileArgs) -> ReadFileResult:
        """Read a file from disk and return its content + sha256.

        Args:
            args: :class:`ReadFileArgs` with the file path.

        Returns:
            :class:`ReadFileResult` with path, content, and sha256.

        Raises:
            FileNotFoundError: If the path does not exist.
            IsADirectoryError: If the path is a directory.
        """
        path = Path(args.path)
        content = path.read_text(encoding="utf-8")
        return ReadFileResult(
            path=args.path,
            content=content,
            sha256=_sha256(content),
        )

    # ------------------------------------------------------------------
    # edit_file
    # ------------------------------------------------------------------

    async def edit_file(self, args: EditFileArgs) -> EditFileResult:
        """Edit a file using exact string replacement.

        Mirrors Claude Code's Edit tool semantics: ``old_str`` must appear
        exactly once in the file so the edit is unambiguous.  An optional
        ``expected_sha256`` guards against stale edits — if the file has
        changed since the agent read it, the edit is refused.

        Args:
            args: :class:`EditFileArgs` with path, old_str, new_str, and
                  optional expected_sha256.

        Returns:
            :class:`EditFileResult` with path, new_sha256, and edits_applied (1).

        Raises:
            ValueError: If old_str appears zero times or more than once, or if
                        expected_sha256 does not match the current file content.
            FileNotFoundError: If the path does not exist.
        """
        path = Path(args.path)
        content = path.read_text(encoding="utf-8")

        # SHA guard — refuse if file has changed since agent read it.
        if args.expected_sha256 is not None:
            current_sha = _sha256(content)
            if current_sha != args.expected_sha256:
                raise ValueError(
                    f"edit_file sha guard failed for {args.path!r}: "
                    f"expected sha256={args.expected_sha256!r} but current file "
                    f"has sha256={current_sha!r}. Re-read the file before editing."
                )

        count = content.count(args.old_str)
        if count == 0:
            raise ValueError(
                f"edit_file: old_str not found in {args.path!r}. "
                "Check that the string matches exactly (whitespace, indentation)."
            )
        if count > 1:
            raise ValueError(
                f"edit_file: old_str appears {count} times in {args.path!r}. "
                "Provide a longer, unique context string to pinpoint the edit location."
            )

        new_content = content.replace(args.old_str, args.new_str, 1)
        path.write_text(new_content, encoding="utf-8")
        new_sha = _sha256(new_content)

        return EditFileResult(
            path=args.path,
            new_sha256=new_sha,
            edits_applied=1,
        )


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def tool_specs() -> list[dict]:
    """Return JSON-serializable tool definitions for the intervention prompt.

    Each entry has:
    - ``name``:        tool name
    - ``description``: one-line summary
    - ``schema``:      JSON Schema object (from pydantic's model_json_schema())
    """
    return [
        {
            "name": "rewind",
            "description": (
                "Invalidate completed events back to one or more target event IDs "
                "and prepare the workflow for re-execution from that point."
            ),
            "schema": RewindArgs.model_json_schema(),
        },
        {
            "name": "resume",
            "description": (
                "Signal that the workflow is ready to resume. "
                "The repair CLI will spawn `godel resume <run_id>` after the "
                "intervention workflow returns."
            ),
            "schema": ResumeArgs.model_json_schema(),
        },
        {
            "name": "input",
            "description": (
                "Inject a value into the paused input() call so that on resume "
                "the workflow receives the value without blocking on stdin."
            ),
            "schema": InputArgs.model_json_schema(),
        },
        {
            "name": "give_up",
            "description": (
                "Declare the intervention failed and mark the run as unrecoverable. "
                "Writes an UNRECOVERABLE event to the audit log."
            ),
            "schema": GiveUpArgs.model_json_schema(),
        },
        {
            "name": "read_file",
            "description": "Read a file and return its content and sha256 digest.",
            "schema": ReadFileArgs.model_json_schema(),
        },
        {
            "name": "edit_file",
            "description": (
                "Edit a file using exact string replacement (old_str → new_str). "
                "old_str must appear exactly once. Optionally guard with expected_sha256."
            ),
            "schema": EditFileArgs.model_json_schema(),
        },
    ]

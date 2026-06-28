"""Exception hierarchy for godel errors.

Two root hierarchies exist — they are NOT the same despite the similar names:

* ``GodelStrictError`` — raised by the **static strict-mode guards** (AST,
  import, and audit layers) when they detect non-deterministic constructs at
  analysis time.  It is *not* raised by the engine itself.

* ``GodelError`` — base for **all structured errors** produced by the godel
  engine: runtime errors during workflow execution (agent failures, schema
  mismatches, timeouts, etc.), configuration errors raised at decoration or
  parallel-call time (``ConfigError``), and resume/replay errors
  (``ResumeError`` and its subclasses).  Using ``except GodelError`` is
  therefore a reliable catch-all for any godel-originated failure.

  ``GodelStrictError`` deliberately sits *outside* this hierarchy because it
  is a static analysis signal, not a workflow operation failure.
"""
from __future__ import annotations

from dataclasses import dataclass

from typing_extensions import TypedDict, Unpack


@dataclass
class StrictViolation:
    """A single violation detected by strict mode guards."""
    file: str
    line: int
    col: int
    message: str
    layer: str  # 'ast' | 'import' | 'audit'


# ---------------------------------------------------------------------------
# WorkflowFail — base for workflow-level failures.
# Canonical home; re-exported from _decorators for backward compatibility.
# ---------------------------------------------------------------------------

class WorkflowFail(Exception):
    pass


# ---------------------------------------------------------------------------
# Internal control-flow signals — NOT user-facing errors.
# These are raised and caught by godel internals to drive workflow state
# transitions (rewind, pause).  They remain plain Exception subclasses so
# that accidental ``except GodelError`` blocks never swallow them.
# ---------------------------------------------------------------------------

class RewindSignal(Exception):
    """Raised by rewind() to unwind the workflow call stack.
    Caught by the @workflow decorator, which applies the graph cut
    and re-invokes the workflow function with a new ReplayWalker.
    This is NOT a user-facing error — it's a control flow signal.
    """
    def __init__(self, target_ids: list[str], reason: str = ""):
        self.target_ids = target_ids
        self.reason = reason
        super().__init__(f"RewindSignal to {target_ids}: {reason}")


class PauseSignal(Exception):
    """Raised inside a @step wrapper when a pause request is detected.

    Caught by @workflow, which emits a PAUSED event and exits cleanly.
    Not a user-facing error — a control flow signal.
    """
    def __init__(self, reason: str = "", request_ts: str = ""):
        self.reason = reason
        self.request_ts = request_ts
        super().__init__(f"PauseSignal: {reason}")


# ---------------------------------------------------------------------------
# Static-analysis error — outside the GodelError hierarchy by design.
# ---------------------------------------------------------------------------

class GodelStrictError(Exception):
    """Raised when the **strict-mode guards** detect non-deterministic
    constructs (AST, import, or audit layer violations).

    This is a *static analysis / pre-flight* error — it is raised before the
    workflow runs, never during execution.  See ``GodelError`` for the runtime
    error hierarchy.
    """

    def __init__(self, violations: list[StrictViolation], message: str = ""):
        self.violations = violations
        if not message:
            message = f"godel strict: {len(violations)} violation(s) detected"
        super().__init__(message)

    def __str__(self) -> str:
        lines = [f"GodelStrictError: {len(self.violations)} violation(s)"]
        for v in self.violations:
            loc = f"{v.file}:{v.line}:{v.col}" if v.line > 0 else v.file
            lines.append(f"  [{v.layer}] {loc} — {v.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# GodelError — base for ALL structured godel errors.
# ---------------------------------------------------------------------------

def _render_context_marker(
    step_path: tuple[str, ...],
    source_location: str,
    remediation_hint: str,
) -> str:
    """Shared helper: build the ``[godel:...]`` context marker string.

    Returns an empty string when all inputs are empty / falsy.

    Empty-string and whitespace-only components in *step_path* are stripped so
    that a path like ``('',)``, ``('   ',)``, or ``('a', '', 'b')`` never
    renders as ``step=``, ``step=   ``, or ``step=a//b``.

    Used by both :class:`GodelError` and :class:`~godel._run.CommandFailure`
    so that the marker format stays in sync across both hierarchies.
    """
    parts = []
    clean_path = tuple(s for s in step_path if s and s.strip())
    if clean_path:
        parts.append(f"step={'/'.join(clean_path)}")
    if source_location:
        parts.append(f"source={source_location}")
    if remediation_hint:
        parts.append(f"hint={remediation_hint}")
    if not parts:
        return ""
    return "[godel:" + ", ".join(parts) + "]"


# ---------------------------------------------------------------------------
# CommandFailure hierarchy — WorkflowFail subclasses for CLI failures.
# Canonical home; re-exported from _run for backward compatibility.
# ---------------------------------------------------------------------------

class CommandFailure(WorkflowFail):
    """Raised when a ``run()`` call exits with a non-zero return code or times out.

    Inherits from :class:`WorkflowFail` so that the ``@workflow`` decorator
    catches it as a recognised workflow-level failure.  Also carries the same
    structured context fields as :class:`GodelError`
    (``step_path``, ``source_location``, ``remediation_hint``) and reuses its
    ``_context_marker`` / ``__str__`` logic via a shared helper.

    .. note::
        ``CommandFailure`` is intentionally **not** a subclass of
        :class:`GodelError` — it lives in the ``WorkflowFail`` hierarchy so
        that callers catching either branch work correctly.  The structured
        context is provided by delegating to :func:`_render_context_marker`.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        step_path: tuple[str, ...] = (),
        source_location: str = "",
        remediation_hint: str = "",
    ):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.step_path = step_path
        self.source_location = source_location
        self.remediation_hint = remediation_hint

    def _context_marker(self) -> str:
        return _render_context_marker(self.step_path, self.source_location, self.remediation_hint)

    def __str__(self) -> str:
        base = super().__str__()
        marker = self._context_marker()
        if marker:
            return f"{base} {marker}" if base else marker
        return base


class ContextOverflowError(CommandFailure):
    """Raised when an agent's CLI session exceeds the model's context window.

    Inherits from :class:`CommandFailure` so that ``except CommandFailure``
    catches it generically, while ``except ContextOverflowError`` allows
    targeted handling::

        try:
            result = await agent("classify this item")
        except ContextOverflowError:
            await agent.compact()        # reduce context, retry
            result = await agent("classify this item")

    Or create a fresh agent::

        try:
            result = await agent("classify this item")
        except ContextOverflowError:
            agent = claude_code(model="sonnet")
            result = await agent("classify this item")

    Attributes:
        model: The model that hit the limit.
        session_id: The session that overflowed (if available).
    """

    def __init__(
        self,
        message: str = "",
        *,
        model: str = "",
        session_id: str | None = None,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        step_path: tuple[str, ...] = (),
        source_location: str = "",
        remediation_hint: str = "",
    ):
        super().__init__(
            message,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            step_path=step_path,
            source_location=source_location,
            remediation_hint=remediation_hint,
        )
        self.model = model
        self.session_id = session_id


# ---------------------------------------------------------------------------
# GodelError — base for ALL structured godel errors.
# ---------------------------------------------------------------------------

class _GodelErrorKwargs(TypedDict, total=False):
    """Keyword arguments shared by all :class:`GodelError` subclasses.

    Using ``Unpack[_GodelErrorKwargs]`` in subclass ``__init__`` signatures
    restores IDE / type-checker visibility for these arguments, which would
    otherwise be hidden behind an opaque ``**kwargs``.
    """

    step_path: tuple[str, ...]
    source_location: str
    remediation_hint: str


class GodelError(Exception):
    """Base class for all structured Godel errors.

    Covers runtime failures during workflow execution (agent failures, schema
    mismatches, timeouts, etc.), configuration errors (``ConfigError``), and
    resume/replay errors (``ResumeError`` family).  Every subclass provides
    enough context for an LLM to diagnose the failure without additional log
    scraping.

    .. note::
        ``GodelStrictError`` (static analysis guard) and the internal
        control-flow signals ``RewindSignal`` / ``PauseSignal`` are **not**
        subclasses of ``GodelError``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        step_path: tuple[str, ...] = (),
        source_location: str = "",
        remediation_hint: str = "",
    ):
        super().__init__(message)
        self.step_path = step_path
        self.source_location = source_location
        self.remediation_hint = remediation_hint

    def _context_marker(self) -> str:
        """Return a structured marker string for LLM-readable context.

        Delegates to :func:`_render_context_marker` — see that function for
        the full format specification.
        """
        return _render_context_marker(self.step_path, self.source_location, self.remediation_hint)

    def __str__(self) -> str:
        base = super().__str__()
        marker = self._context_marker()
        if marker:
            return f"{base} {marker}" if base else marker
        return base


# ---------------------------------------------------------------------------
# Resume / replay errors — GodelError subclasses.
# ---------------------------------------------------------------------------

class ResumeError(GodelError):
    """General resume failure — corrupted log, missing WORKFLOW_STARTED,
    request_hash mismatch with abort policy.

    Inherits from :class:`GodelError` so that ``except GodelError`` catch-alls
    cover resume failures alongside runtime errors.
    """
    pass


class SourceEditedError(ResumeError):
    """Raised when a cached step's source has been edited and the policy is ABORT.

    Indicates that a @step function body was changed after it already
    completed and was recorded in the event log.  Replaying the cached
    result would diverge from the current code.

    Fix: run ``godel rewind --to <event_id>`` to invalidate the cached
    result, then re-run or resume normally.
    """
    def __init__(
        self,
        message: str = "",
        *,
        step_name: str = "",
        event_id: str = "",
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.step_name = step_name
        self.event_id = event_id

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base] if base else []
        if self.step_name:
            parts.append(f"  Step: {self.step_name}")
        if self.event_id:
            parts.append(f"  Cached event: {self.event_id}")
        parts.append("")
        parts.append("  Fix: run `godel rewind --to <event_id>` to invalidate the")
        parts.append("  cached result, then resume normally.")
        return "\n".join(parts)


class UnsafeResumeError(ResumeError):
    """Raised when a non-idempotent run() is in STARTED-only state.

    The command may have partially executed with irreversible side effects.
    Cannot safely re-execute without explicit idempotent=True.
    """
    def __init__(
        self,
        message: str,
        *,
        event_id: str = "",
        cmd: str | list[str] = "",
        step_path: tuple[str, ...] = (),
        source_location: str = "",
        remediation_hint: str = "",
    ):
        super().__init__(
            message,
            step_path=step_path,
            source_location=source_location,
            remediation_hint=remediation_hint,
        )
        self.event_id = event_id
        if isinstance(cmd, list):
            from godel._run import _cmd_display
            self.cmd = _cmd_display(cmd)
        else:
            self.cmd = cmd

    def _context_marker(self) -> str:
        # UnsafeResumeError renders step/command context in its own __str__
        # format; suppress the GodelError [godel:...] marker to avoid
        # duplicate output.
        return ""

    def __str__(self) -> str:
        parts = [f"UnsafeResumeError: {super().__str__()}"]
        if self.cmd:
            parts.append(f"  Command: {self.cmd}")
        if self.step_path:
            parts.append(f"  Step: {'/'.join(self.step_path)}")
        parts.append("")
        parts.append("  Fix: mark the run() call as idempotent=True if safe to retry,")
        parts.append("  or use godel rewind to back up past this operation.")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Configuration error — GodelError subclass.
# ---------------------------------------------------------------------------

class ConfigError(GodelError):
    """Raised when a decorator is configured with an invalid combination of options.

    This is a **pre-execution** error — raised either at decoration time
    (module import / function definition) or when the workflow primitive that
    owns the invalid configuration is entered, but always *before* any step
    body runs.  No partial state is produced.

    Inherits from :class:`GodelError` so that ``except GodelError`` catch-alls
    cover configuration failures alongside runtime and resume errors.

    When each check fires:

    * ``@workflow(redact=[...])`` with a non-callable entry or a callable
      whose arity is not a single positional argument → ``TypeError`` at
      **decoration time** (module import).  ``TypeError`` is used instead of
      ``ConfigError`` to match Python convention for argument-type errors.
    * ``@step(capture_stdout=True)`` used inside a ``parallel()`` block →
      ``ConfigError`` raised at **parallel-call time**, immediately upon
      entering ``parallel()`` and before any branch is scheduled.  This check
      cannot fire at decoration time because the step function and the
      enclosing ``parallel()`` call are independently defined; the relationship
      only becomes visible when the coroutines are handed to ``parallel()``.
    """


# ---------------------------------------------------------------------------
# Runtime GodelError subclasses.
# ---------------------------------------------------------------------------

class AgentRefusal(GodelError):
    """Raised when an AI model refuses to fulfil a request."""

    def __init__(
        self,
        message: str = "",
        *,
        model: str = "",
        refusal_reason: str = "",
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.model = model
        self.refusal_reason = refusal_reason


class SchemaValidationFailure(GodelError):
    """Raised when an agent response fails schema validation.

    .. note::
        A separate :class:`~godel.agents._claude.SchemaValidationFailure`
        exists in ``godel.agents._claude`` as a lightweight
        :class:`~godel._decorators.WorkflowFail` subclass used internally
        when parsing Claude CLI output.  That class is **not** the same as
        this one — it does not carry structured context fields and is not
        exported from the public ``godel`` namespace.
    """

    def __init__(
        self,
        message: str = "",
        *,
        schema_name: str = "",
        validation_errors: list[str] | None = None,
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.schema_name = schema_name
        self.validation_errors: list[str] = validation_errors if validation_errors is not None else []


class HumanTimeout(GodelError):
    """Raised when a blocking PROMPT call times out waiting for human input.

    Attributes:
        prompt: The prompt text that was waiting for a response.
        timeout_seconds: How long the call waited before timing out, in
            seconds.  ``None`` means the duration was not recorded (e.g. the
            timeout was imposed externally or the value was unavailable at
            raise time).  Code that reads this field **must** guard against
            ``None`` before performing any arithmetic — do not assume it is
            a ``float``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        prompt: str = "",
        timeout_seconds: float | None = None,
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.prompt = prompt
        self.timeout_seconds = timeout_seconds


class NonDeterministicEscape(GodelError):
    """Raised at **runtime** when an operation would introduce non-determinism.

    This is a runtime error produced by the execution engine (e.g. when an
    intercepted stdlib call is invoked outside a :func:`det` context).  It is
    *not* raised by the strict-mode linter/AST guards — those raise
    :class:`GodelStrictError` instead.
    """

    def __init__(
        self,
        message: str = "",
        *,
        operation: str = "",
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.operation = operation


class GodelWatchNotInstalledError(ImportError):
    """Raised when ``godel --watch`` is used without the ``watch`` extra.

    Install the missing dependency with::

        pip install 'godel[watch]'
    """


class RewindUnsafe(GodelError):
    """Raised when a rewind operation cannot be performed safely.

    This is a **pre-flight** guard raised by :func:`godel.rewind` *before* any
    graph mutations occur.  It means the requested cut-point would invalidate
    a non-idempotent ``run()`` event, so the rewind is refused outright.

    **Contrast with** :class:`UnsafeResumeError`, which is raised *during
    replay* when the engine encounters a ``run()`` event that is stuck in
    ``STARTED``-only state (i.e. the process was interrupted mid-execution and
    the command may have partially run with irreversible side-effects).
    ``RewindUnsafe`` → rewind safety check.  ``UnsafeResumeError`` → resume
    safety check.  They guard different phases of the workflow lifecycle.
    """

    def __init__(
        self,
        message: str = "",
        *,
        event_id: str = "",
        op: str = "",
        cmd: str | list[str] | None = None,
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.event_id = event_id
        self.op = op
        if isinstance(cmd, list):
            from godel._run import _cmd_display
            self.cmd: str | None = _cmd_display(cmd)
        else:
            self.cmd = cmd


# ---------------------------------------------------------------------------
# Per-step wall-clock timeout error — GodelError subclass.
# ---------------------------------------------------------------------------

class StepTimeout(GodelError):
    """Raised when a ``@step(timeout=N)`` body exceeds N seconds.

    The step is cancelled via ``asyncio.wait_for``; its event log entry is
    emitted as FAILED with ``error_type='StepTimeout'``.

    Compose with ``@retry`` to automatically retry timed-out steps::

        @retry(times=3)
        @step(timeout=10)
        async def fetch_data():
            ...

    Attributes:
        step_name: Name of the step that timed out.
        timeout_seconds: The configured timeout limit in seconds.
    """

    def __init__(
        self,
        message: str = "",
        *,
        step_name: str = "",
        timeout_seconds: float | None = None,
        **kwargs: Unpack[_GodelErrorKwargs],
    ):
        super().__init__(message, **kwargs)
        self.step_name = step_name
        self.timeout_seconds = timeout_seconds

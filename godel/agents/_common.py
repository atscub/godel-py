"""Shared types and base class for godel agent factories.

SchemaValidationFailure is kept here so every agent implementation raises the
*same* class, making ``isinstance(err, godel.agents.SchemaValidationFailure)``
work regardless of which agent raised it.

_BaseAgent implements the template shared by all CLI-backed agents:
    * event-log lifecycle around each call
    * prompt augmentation with a JSON schema for structured output
    * raw / fenced JSON coercion
    * natural-language extraction fallback via a cheaper model
    * session persistence across repeated calls on the same agent instance

Subclasses override the small pieces that actually vary per CLI:
    * ``_model_aliases`` — map user-facing aliases to CLI model ids
    * ``_extraction_model`` — cheap model id for the extraction fallback
    * ``_build_command`` — assemble the shell command, given tools & session id
    * ``_parse_output`` — return ``(text, session_id | None)`` from stdout
"""
from __future__ import annotations

import asyncio
import io
import json
import re
from typing import TYPE_CHECKING, Type, TypeVar, overload

from pydantic import BaseModel, ValidationError

from godel._decorators import WorkflowFail
from godel._run import run

if TYPE_CHECKING:
    from godel._transcript import TranscriptWriter
    from godel.agents._adapters import ClaudeAdapter, CopilotAdapter

T = TypeVar("T", bound=BaseModel)


def stream_into_transcript(
    stdout_bytes: bytes,
    transcript: "TranscriptWriter",
    step_path: tuple,
    stream_path: list,
    adapter: "ClaudeAdapter | CopilotAdapter",
) -> None:
    """Feed *stdout_bytes* through the tolerant parser and emit events to *transcript*.

    Each successfully parsed JSON object is passed to *adapter*.map().  If the
    adapter returns a ``(op, extra)`` tuple, a transcript event is written.
    If it returns ``None``, the parsed item is silently skipped (metadata-only).

    ``Raw`` items (malformed / oversized lines) are emitted as ``"agent.raw"``
    events unconditionally so that vendor drift is observable without crashing.

    Parameters
    ----------
    stdout_bytes:
        The raw subprocess stdout captured during an agent call, as bytes.
    transcript:
        The open :class:`~godel._transcript.TranscriptWriter` to write events to.
    step_path:
        The step path at the time of the agent call (for event correlation).
    stream_path:
        The stream path stamped on the ``agent.call`` event (for correlation).
    adapter:
        A vendor-specific adapter instance with a ``map(data) -> (op, extra) | None``
        method.
    """
    from godel.agents._stream_parser import Parsed, Raw, iter_parsed

    reader = io.BytesIO(stdout_bytes)
    for item in iter_parsed(reader):
        if isinstance(item, Parsed):
            result = adapter.map(item.data)
            if result is not None:
                for op, extra in result:
                    transcript.write_event(
                        op,
                        step_path=step_path,
                        stream_path=stream_path,
                        **extra,
                    )
        elif isinstance(item, Raw):
            transcript.write_event(
                "agent.raw",
                step_path=step_path,
                stream_path=stream_path,
                text=item.text,
                reason=item.reason,
            )


class AdapterStreamSink:
    """Bridges the run() line observer to a vendor adapter and transcript.

    Holds a partial-line buffer (for mid-line flushes, though rare in JSONL)
    and a :class:`~godel.agents._stream_parser.StreamingParser` that classifies
    each line via *adapter*.map().  Classified results are written to
    *transcript* as canonical agent events (``agent.thought``, ``agent.tool_call``,
    ``agent.tool_result``, ``agent.raw``).

    Usage::

        sink = AdapterStreamSink(adapter, transcript, step_path, stream_path)
        token = _line_observer.set(sink.feed)
        try:
            result = await run(cmd)
        finally:
            _line_observer.reset(token)
            sink.close()   # flushes any trailing partial line
    """

    def __init__(
        self,
        adapter: "ClaudeAdapter | CopilotAdapter",
        transcript: "TranscriptWriter",
        step_path: tuple,
        stream_path: list,
    ) -> None:
        from godel.agents._stream_parser import StreamingParser

        self._adapter = adapter
        self._transcript = transcript
        self._step_path = step_path
        self._stream_path = stream_path
        self._parser = StreamingParser()

    def feed(self, line: bytes) -> None:
        """Receive one raw line (bytes, including trailing newline) from run()."""
        from godel.agents._stream_parser import Parsed, Raw

        for item in self._parser.feed(line):
            self._emit(item)

    def close(self) -> None:
        """Flush any remaining partial line from the internal buffer."""
        from godel.agents._stream_parser import Parsed, Raw

        for item in self._parser.close():
            self._emit(item)

    def _emit(self, item: "Parsed | Raw") -> None:
        from godel.agents._stream_parser import Parsed, Raw

        if isinstance(item, Parsed):
            result = self._adapter.map(item.data)
            if result is not None:
                for op, extra in result:
                    self._transcript.write_event(
                        op,
                        step_path=self._step_path,
                        stream_path=self._stream_path,
                        **extra,
                    )
        elif isinstance(item, Raw):
            self._transcript.write_event(
                "agent.raw",
                step_path=self._step_path,
                stream_path=self._stream_path,
                text=item.text,
                reason=item.reason,
            )


class SchemaValidationFailure(WorkflowFail):
    """Raised when an agent response cannot be coerced to the requested schema."""

    def __init__(self, message: str, *, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def _extract_json_block(text: str) -> str | None:
    """Extract JSON from a markdown ```json ... ``` fence if present."""
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1).strip() if m else None


class _BaseAgent:
    _model_aliases: dict[str, str] = {}
    _extraction_model: str = ""

    def __init__(
        self,
        model: str,
        cwd: str | None,
        tools: list[str] | None,
        skip_permissions: bool,
        system_prompt: str | None = None,
    ):
        self._model = model
        self._cwd = cwd
        self._tools = tools
        self._skip_permissions = skip_permissions
        self._session_id: str | None = None
        # Normalise the system prompt: strip whitespace and treat empty / all
        # whitespace as "no system prompt at all" so we never prepend bare
        # whitespace into the first user prompt.
        if system_prompt is not None:
            stripped = system_prompt.strip()
            self._system_prompt: str | None = stripped or None
        else:
            self._system_prompt = None
        # Tracks whether the system_prompt has been successfully sent to the
        # CLI. Flipped to True only AFTER a successful agent call returns, so
        # a first-call failure leaves the prompt available for retry.
        self._system_prompt_sent: bool = False
        # Agents are conversational: a single instance must serialize its
        # calls so session state stays coherent under PARALLEL / gather().
        self._lock = asyncio.Lock()

    @overload
    async def __call__(self, prompt: str) -> str: ...
    @overload
    async def __call__(self, prompt: str, *, schema: Type[T]) -> T: ...

    async def __call__(self, prompt: str, *, schema=None):
        from godel._context import _current_workflow, _current_stream_path
        from ulid import ULID

        ctx = _current_workflow.get()

        # Stamp stream_path at agent-call launch time on the calling thread.
        # Same pattern as run(): read parent path here, append a fresh ULID,
        # and set the contextvar so any nested run() calls inside the agent
        # produce depth-2+ stream_paths.
        parent_stream_path = _current_stream_path.get()
        launch_id = str(ULID())
        new_stream_path = parent_stream_path + [launch_id]
        stream_path_token = _current_stream_path.set(new_stream_path)

        # The system-prompt prepend + event emission + CLI invocation all
        # happen inside self._lock so that under concurrent gather()/PARALLEL
        # the briefing is prepended exactly once (whichever task acquires the
        # lock first sees _system_prompt_sent=False, prepends, and — after
        # success — flips the flag; the rest see the flipped flag).
        event = None
        prepended_system_prompt = False
        try:
            try:
                async with self._lock:
                    # Apply the system-prompt prepend INSIDE the lock so the
                    # audit log records the exact prompt sent to the CLI
                    # and so concurrent calls cannot each re-prepend.
                    # The flag is NOT flipped here — we must wait until the
                    # CLI call succeeds (see below) so a first-call failure
                    # leaves the briefing available for retry.
                    #
                    # Resume guard: if _session_id is already set (either
                    # because a prior successful call populated it, or because
                    # workflow replay restored it), the briefing was already
                    # delivered in that session — skip the prepend regardless
                    # of _system_prompt_sent.  This prevents double-delivery
                    # when a workflow resumes mid-run.
                    if self._system_prompt and not self._system_prompt_sent and not self._session_id:
                        prompt = f"{self._system_prompt}\n\n{prompt}"
                        prepended_system_prompt = True

                    if ctx and ctx.event_log:
                        request_data = {
                            "model": self._model,
                            "prompt": prompt[:500],
                            "has_schema": schema is not None,
                            "schema_name": schema.__name__ if schema else None,
                            "session_id": self._session_id,
                        }
                        event = ctx.event_log.emit_started(
                            op="agent.call",
                            step_path=tuple(ctx.step_stack),
                            request=request_data,
                            stream_path=new_stream_path,
                        )

                    result = await self._execute(prompt, schema=schema)
            except (Exception, asyncio.CancelledError) as exc:
                # NOTE: scope is intentionally (Exception, CancelledError) and
                # NOT BaseException — we deliberately let KeyboardInterrupt
                # and SystemExit propagate untouched so process-signal teardown
                # is not polluted by spurious FAILED entries.  CancelledError
                # is called out explicitly because in Python 3.8+ it is a
                # BaseException (not an Exception), so a plain `except Exception`
                # silently left the agent.call event stuck in STARTED.
                if event:
                    try:
                        import traceback as _tb
                        tb_frames = _tb.extract_tb(exc.__traceback__)
                        source_loc = ""
                        if tb_frames:
                            last = tb_frames[-1]
                            source_loc = f"{last.filename}:{last.lineno}"
                        ctx.event_log.emit_failed(
                            event.event_id,
                            str(exc),
                            error_type=type(exc).__name__,
                            source_location=source_loc,
                        )
                    except Exception:
                        # Logging must never swallow the original failure.
                        # If emit_failed itself raises (closed file, disk full,
                        # serialisation error, etc.) we drop the logging error
                        # and fall through to re-raise the original exception
                        # below, so callers always see the real cause.
                        pass
                raise

            if event:
                response_data = {
                    "type": "structured" if schema else "text",
                    "value": repr(result)[:500],
                    "session_id": self._session_id,
                }
                ctx.event_log.emit_finished(event.event_id, response=response_data)

            # Only mark the system prompt as sent after a successful call;
            # this way, if the first invocation raises (network error,
            # CLI crash, etc.) the next retry still gets the briefing.
            if prepended_system_prompt:
                self._system_prompt_sent = True

            return result
        finally:
            _current_stream_path.reset(stream_path_token)

    async def _execute(self, prompt: str, *, schema=None):
        model_id = self._model_aliases.get(self._model, self._model)
        # Note: the system-prompt prepend lives in __call__() so that:
        #   (a) the agent.call event log records the exact prompt sent to
        #       the CLI (auditability), and
        #   (b) the flag is flipped only after a successful call, letting
        #       retries after a first-call failure still carry the briefing.
        full_prompt = prompt
        if schema is not None:
            schema_json = json.dumps(schema.model_json_schema(), indent=2)
            full_prompt = (
                f"{prompt}\n\n"
                f"IMPORTANT: After completing the task, your FINAL response must be ONLY "
                f"a JSON object matching this schema (no markdown, no explanation, just raw JSON):\n"
                f"{schema_json}"
            )

        text = await self._invoke(
            full_prompt, model_id, tools=self._tools, persist_session=True
        )

        if schema is None:
            return text

        for candidate in [text, _extract_json_block(text)]:
            if candidate is None:
                continue
            try:
                parsed = json.loads(candidate)
                return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError):
                continue

        # Fallback: agent result is natural language. Extract structured data
        # with a cheap, isolated call — no tools, no session continuity.
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        extract_prompt = (
            f"Extract the following information from this text and return ONLY "
            f"a JSON object matching the schema. No markdown fences, no explanation, "
            f"just the raw JSON object.\n\n"
            f"Schema:\n{schema_json}\n\n"
            f"Text:\n{text}"
        )
        extract_text = await self._invoke(
            extract_prompt, self._extraction_model, tools=[], persist_session=False
        )

        for candidate in [extract_text, _extract_json_block(extract_text)]:
            if candidate is None:
                continue
            try:
                parsed = json.loads(candidate)
                return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError):
                continue

        raise SchemaValidationFailure(
            f"failed to parse response as {schema.__name__}",
            raw=text,
        )

    async def _invoke(
        self,
        prompt: str,
        model_id: str,
        *,
        tools: list[str] | None,
        persist_session: bool,
    ) -> str:
        """Run one CLI call and return the assistant text.

        When ``persist_session`` is True, the session id from the response
        (if any) is stored on the instance so the next call can resume it.
        When agent streaming is enabled (the default; disabled only via
        ``godel run --no-stream`` or ``GODEL_STREAM_AGENTS=0``), an
        :class:`AdapterStreamSink` is installed as the ``_line_observer`` so
        that ``agent.thought`` / ``agent.tool_call`` / ``agent.tool_result``
        events are written to the workflow transcript in real-time, one line
        at a time, rather than post-hoc from the full stdout buffer.
        """
        from godel._context import _current_workflow, _current_stream_path, _line_observer

        ctx = _current_workflow.get()
        streaming = ctx is not None and ctx.stream_agents and ctx.transcript is not None

        session_id = self._session_id if persist_session else None
        cmd = self._build_command(
            prompt, model_id, tools=tools, session_id=session_id,
            streaming=streaming,
        )

        sink = None
        observer_token = None
        step_path = tuple(ctx.step_stack) if ctx else ()
        stream_path = list(_current_stream_path.get()) if streaming else []
        if streaming:
            sink = AdapterStreamSink(
                self._make_adapter(),
                ctx.transcript,
                step_path=step_path,
                stream_path=stream_path,
            )
            observer_token = _line_observer.set(sink.feed)
            # Surface the prompt as its own transcript event so watchers can
            # pair input → output.  Without this the user only ever sees the
            # agent's reply, breaking continuity.
            ctx.transcript.write_event(
                "agent.prompt",
                step_path=step_path,
                stream_path=stream_path,
                model=model_id,
                prompt=prompt,
                session_id=self._session_id,
            )

        try:
            result = await run(cmd, cwd=self._cwd)
        finally:
            if sink is not None and observer_token is not None:
                _line_observer.reset(observer_token)
                sink.close()

        text, new_session_id = self._parse_output(result.stdout)
        if streaming:
            ctx.transcript.write_event(
                "agent.response",
                step_path=step_path,
                stream_path=stream_path,
                model=model_id,
                text=text,
            )
        if persist_session and new_session_id:
            self._session_id = new_session_id
        return text

    def _build_command(
        self,
        prompt: str,
        model_id: str,
        *,
        tools: list[str] | None,
        session_id: str | None,
        streaming: bool = False,
    ) -> str:
        """Build the shell command for one CLI invocation.

        ``tools`` is ``None`` to accept the CLI's default tool policy,
        an empty list to explicitly disable all tools (extraction calls),
        or a non-empty list of tool names to allow.

        ``session_id`` is the id of an existing session to resume, or
        ``None`` to start a fresh session.

        ``streaming`` is ``True`` when the caller wants granular event
        streaming; subclasses may append CLI flags (e.g. Claude's
        ``--output-format stream-json``) when this is set.
        """
        raise NotImplementedError

    def _make_adapter(self):
        """Return the vendor-specific adapter instance for this agent.

        Subclasses must override this to return an instance of their
        corresponding adapter (e.g. ``ClaudeAdapter`` or ``CopilotAdapter``).
        """
        raise NotImplementedError

    def _parse_output(self, stdout: str) -> tuple[str, str | None]:
        """Extract assistant text and session id from CLI stdout.

        Default: treat the whole stdout as plain text and return no session id.
        Subclasses override to parse their CLI's structured output.
        """
        return stdout.strip(), None

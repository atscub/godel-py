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
import json
import re
import sys
from typing import Type, TypeVar, overload

from pydantic import BaseModel, ValidationError

from godel._decorators import WorkflowFail

T = TypeVar("T", bound=BaseModel)


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
    ):
        self._model = model
        self._cwd = cwd
        self._tools = tools
        self._skip_permissions = skip_permissions
        self._session_id: str | None = None
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

        event = None
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

        try:
            try:
                async with self._lock:
                    result = await self._execute(prompt, schema=schema)
            except Exception as exc:
                if event:
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
                raise

            if event:
                response_data = {
                    "type": "structured" if schema else "text",
                    "value": repr(result)[:500],
                    "session_id": self._session_id,
                }
                ctx.event_log.emit_finished(event.event_id, response=response_data)

            return result
        finally:
            _current_stream_path.reset(stream_path_token)

    async def _execute(self, prompt: str, *, schema=None):
        model_id = self._model_aliases.get(self._model, self._model)
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
        """
        session_id = self._session_id if persist_session else None
        cmd = self._build_command(prompt, model_id, tools=tools, session_id=session_id)
        run = sys.modules[type(self).__module__].run
        result = await run(cmd, cwd=self._cwd)
        text, new_session_id = self._parse_output(result.stdout)
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
    ) -> str:
        """Build the shell command for one CLI invocation.

        ``tools`` is ``None`` to accept the CLI's default tool policy,
        an empty list to explicitly disable all tools (extraction calls),
        or a non-empty list of tool names to allow.

        ``session_id`` is the id of an existing session to resume, or
        ``None`` to start a fresh session.
        """
        raise NotImplementedError

    def _parse_output(self, stdout: str) -> tuple[str, str | None]:
        """Extract assistant text and session id from CLI stdout.

        Default: treat the whole stdout as plain text and return no session id.
        Subclasses override to parse their CLI's structured output.
        """
        return stdout.strip(), None

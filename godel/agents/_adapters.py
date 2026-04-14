"""Vendor-specific adapters that map parsed JSONL lines to canonical godel events.

Each adapter's ``map`` method receives a ``dict`` (the parsed payload from
:func:`~godel.agents._stream_parser.iter_parsed`) and returns either:

* A list of ``(op, extra)`` tuples where ``op`` is one of the canonical event
  ops (``"agent.thought"``, ``"agent.tool_call"``, ``"agent.tool_result"``) and
  ``extra`` is a dict of additional fields to merge into the transcript event.
  The list may contain zero, one, or multiple entries — all are emitted.
* ``None`` (or an empty list) if the payload is metadata-only and should not
  produce any events.

``Raw`` items (malformed / oversized / non-object lines) are handled by the
caller and emitted as ``"agent.raw"`` events; adapters only receive dicts.

Canonical event ops
-------------------
``agent.thought``
    A free-text reasoning/message chunk from the assistant.
    Extra fields: ``text`` (str).

``agent.tool_call``
    The assistant is about to invoke a tool.
    Extra fields: ``tool`` (str), ``input`` (dict | None).

``agent.tool_result``
    A tool's output has been returned to the assistant.
    Extra fields: ``tool`` (str), ``output`` (str | dict | None).
"""
from __future__ import annotations

from typing import List, Tuple


# (op: str, extra: dict)
_MappedEvent = Tuple[str, dict]
# Adapters return a list of events (possibly empty) or None
_MapResult = List[_MappedEvent] | None


class ClaudeAdapter:
    """Maps Claude CLI ``--output-format stream-json`` payloads to godel events.

    Claude stream-json events use a top-level ``"type"`` discriminator:

    * ``"assistant"`` with ``content`` blocks of ``"type": "text"``
      → ``agent.thought``
    * ``"assistant"`` with ``content`` blocks of ``"type": "tool_use"``
      → ``agent.tool_call``
    * ``"tool_result"`` (or ``"user"`` with ``tool_result`` content)
      → ``agent.tool_result``
    * Everything else → ``None`` (ignored)

    When an ``"assistant"`` event carries multiple content blocks (e.g. a text
    block followed by a tool_use block), **all** blocks are emitted as separate
    events in order.
    """

    def __init__(self) -> None:
        # Flips to True on the first ``stream_event`` we observe, indicating
        # Claude CLI was launched with ``--include-partial-messages``.  When
        # set, we ignore the redundant batched ``assistant`` events that
        # otherwise echo the same content_block contents we've already
        # streamed as deltas.
        self._has_partials: bool = False

    def map(self, data: dict) -> _MapResult:
        etype = data.get("type")

        if etype == "stream_event":
            self._has_partials = True
            ev = data.get("event") or {}
            ev_type = ev.get("type")
            if ev_type == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "thinking_delta":
                    txt = delta.get("thinking", "")
                    if txt:
                        return [("agent.thought", {"text": txt})]
                elif dtype == "text_delta":
                    txt = delta.get("text", "")
                    if txt:
                        return [("agent.response", {"text": txt})]
                # input_json_delta and signature_delta carry no user-visible
                # content — skip.
                return None
            # content_block_start for tool_use lacks full input (that comes
            # via input_json_delta); we rely on the batched assistant event
            # below to surface the complete tool_call.  Ignore all other
            # stream_event subtypes.
            return None

        if etype == "assistant":
            content = data.get("message", {}).get("content", data.get("content", []))
            # With partial-messages on, text and thinking blocks have
            # already streamed as deltas.  Re-emitting them here would
            # duplicate the content — but tool_use blocks have NOT been
            # streamed yet (their input only materializes at content_block_stop)
            # so we still need this event to surface tool calls.
            if self._has_partials:
                if not isinstance(content, list):
                    return None
                events: list[_MappedEvent] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        events.append(
                            (
                                "agent.tool_call",
                                {
                                    "tool": block.get("name", ""),
                                    "input": block.get("input"),
                                },
                            )
                        )
                return events if events else None
            if not isinstance(content, list):
                return None
            events: list[_MappedEvent] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    # Assistant "text" content is the visible reply — treat it
                    # as a response chunk, not thinking.  Real reasoning lives
                    # in "thinking" blocks (Claude's extended-thinking output).
                    text = block.get("text", "")
                    if text:
                        events.append(("agent.response", {"text": text}))
                elif btype == "thinking":
                    text = block.get("thinking", block.get("text", ""))
                    if text:
                        events.append(("agent.thought", {"text": text}))
                elif btype == "tool_use":
                    events.append((
                        "agent.tool_call",
                        {
                            "tool": block.get("name", ""),
                            "input": block.get("input"),
                        },
                    ))
            return events if events else None

        if etype == "tool_result":
            content = data.get("content", "")
            if isinstance(content, list):
                # Extract text from content blocks
                texts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(t for t in texts if t)
            return [
                (
                    "agent.tool_result",
                    {
                        "tool": data.get("tool_use_id", ""),
                        "output": content,
                    },
                )
            ]

        if etype == "user":
            # Some Claude CLI versions wrap tool results in a "user" message.
            content = data.get("message", {}).get("content", data.get("content", []))
            events: list[_MappedEvent] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        inner = block.get("content", "")
                        if isinstance(inner, list):
                            texts = [
                                b.get("text", "") for b in inner
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            inner = "\n".join(t for t in texts if t)
                        events.append(
                            (
                                "agent.tool_result",
                                {
                                    "tool": block.get("tool_use_id", ""),
                                    "output": inner,
                                },
                            )
                        )
            return events if events else None

        # All other types (init, system, result, etc.) are metadata — ignore.
        return None


class CopilotAdapter:
    """Maps Copilot CLI JSONL payloads to godel events.

    Copilot emits one JSON object per line with a top-level ``"type"`` field:

    * ``"assistant.message"`` (non-ephemeral) → ``agent.thought``
    * ``"tool.execution_start"`` / ``"tool_call"`` / ``"function_call"``
      → ``agent.tool_call``
    * ``"tool.execution_complete"`` / ``"tool_result"`` / ``"function_result"``
      → ``agent.tool_result``
    * ``"result"``, ``"progress"``, ephemeral messages, etc. → ``None``

    Current Copilot CLI (1.0.25+) uses ``tool.execution_start`` /
    ``tool.execution_complete`` with ``toolName`` + ``result.content`` fields.
    The older ``tool_call`` / ``tool_result`` names are kept for
    backward-compatibility with earlier CLI versions and tests.
    """

    def map(self, data: dict) -> _MapResult:
        etype = data.get("type")

        if etype == "assistant.message":
            if data.get("ephemeral"):
                return None
            content = (data.get("data") or {}).get("content", "")
            if content:
                return [("agent.thought", {"text": content})]
            # Empty-content assistant.message may still carry toolRequests,
            # but the subsequent tool.execution_start events are the
            # authoritative source — skip to avoid duplicates.
            return None

        if etype == "tool.execution_start":
            d = data.get("data") or {}
            return [
                (
                    "agent.tool_call",
                    {
                        "tool": d.get("toolName", ""),
                        "input": d.get("arguments"),
                    },
                )
            ]

        if etype == "tool.execution_complete":
            d = data.get("data") or {}
            result = d.get("result")
            if isinstance(result, dict):
                output = result.get("content", result.get("detailedContent", ""))
            else:
                output = result if result is not None else ""
            return [
                (
                    "agent.tool_result",
                    {
                        "tool": d.get("toolCallId", d.get("toolName", "")),
                        "output": output,
                    },
                )
            ]

        if etype in ("tool_call", "function_call"):
            d = data.get("data") or data
            return [
                (
                    "agent.tool_call",
                    {
                        "tool": d.get("name", d.get("function", {}).get("name", "")),
                        "input": d.get("arguments") or d.get("input"),
                    },
                )
            ]

        if etype in ("tool_result", "function_result"):
            d = data.get("data") or data
            return [
                (
                    "agent.tool_result",
                    {
                        "tool": d.get("tool_call_id", d.get("name", "")),
                        "output": d.get("output", d.get("content", "")),
                    },
                )
            ]

        # Metadata events: "result", "progress", "error", etc.
        return None

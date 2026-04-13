"""Vendor-specific adapters that map parsed JSONL lines to canonical godel events.

Each adapter's ``map`` method receives a ``dict`` (the parsed payload from
:func:`~godel.agents._stream_parser.iter_parsed`) and returns either:

* A ``(op, extra)`` tuple where ``op`` is one of the canonical event ops
  (``"agent.thought"``, ``"agent.tool_call"``, ``"agent.tool_result"``) and
  ``extra`` is a dict of additional fields to merge into the transcript event, or
* ``None`` if the payload is metadata-only and should not produce an event.

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

from typing import Tuple


# (op: str, extra: dict)
_MappedEvent = Tuple[str, dict]


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
    """

    def map(self, data: dict) -> _MappedEvent | None:
        etype = data.get("type")

        if etype == "assistant":
            content = data.get("message", {}).get("content", data.get("content", []))
            if not isinstance(content, list):
                return None
            events: list[_MappedEvent] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
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
            # Return the first event; caller iterates adapter per Parsed item.
            # For multi-block messages, only the first event is returned here;
            # the remainder are dropped.  In practice Claude streams one block
            # per event line, so multi-block batches are rare.
            return events[0] if events else None

        if etype == "tool_result":
            content = data.get("content", "")
            if isinstance(content, list):
                # Extract text from content blocks
                texts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(t for t in texts if t)
            return (
                "agent.tool_result",
                {
                    "tool": data.get("tool_use_id", ""),
                    "output": content,
                },
            )

        if etype == "user":
            # Some Claude CLI versions wrap tool results in a "user" message.
            content = data.get("message", {}).get("content", data.get("content", []))
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
                        return (
                            "agent.tool_result",
                            {
                                "tool": block.get("tool_use_id", ""),
                                "output": inner,
                            },
                        )
            return None

        # All other types (init, system, result, etc.) are metadata — ignore.
        return None


class CopilotAdapter:
    """Maps Copilot CLI JSONL payloads to godel events.

    Copilot emits one JSON object per line with a top-level ``"type"`` field:

    * ``"assistant.message"`` (non-ephemeral) → ``agent.thought``
    * ``"tool_call"`` or ``"function_call"`` → ``agent.tool_call``
    * ``"tool_result"`` or ``"function_result"`` → ``agent.tool_result``
    * ``"result"``, ``"progress"``, ephemeral messages, etc. → ``None``
    """

    def map(self, data: dict) -> _MappedEvent | None:
        etype = data.get("type")

        if etype == "assistant.message":
            if data.get("ephemeral"):
                return None
            content = (data.get("data") or {}).get("content", "")
            if content:
                return ("agent.thought", {"text": content})
            return None

        if etype in ("tool_call", "function_call"):
            d = data.get("data") or data
            return (
                "agent.tool_call",
                {
                    "tool": d.get("name", d.get("function", {}).get("name", "")),
                    "input": d.get("arguments") or d.get("input"),
                },
            )

        if etype in ("tool_result", "function_result"):
            d = data.get("data") or data
            return (
                "agent.tool_result",
                {
                    "tool": d.get("tool_call_id", d.get("name", "")),
                    "output": d.get("output", d.get("content", "")),
                },
            )

        # Metadata events: "result", "progress", "error", etc.
        return None

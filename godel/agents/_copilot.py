"""copilot agent factory.

copilot() returns an async callable that wraps the ``copilot`` CLI via run().
Every agent call goes through run() — the single audited escape hatch.

CLI choice
----------
The ``copilot`` binary comes from the ``@github/copilot-cli`` npm package
(GitHub Copilot CLI 1.0.25+).  It supports a genuine non-interactive path::

    copilot -p PROMPT --model MODEL --allow-all-tools --no-color \\
            --output-format json [--resume=SESSION_ID]

Session persistence
-------------------
When ``--output-format json`` is set, the CLI streams one JSON object per line
and terminates with a ``{"type":"result","sessionId":...}`` event.  We capture
``sessionId`` from that event and pass ``--resume=<id>`` on subsequent calls so
the conversation history is preserved.

Extraction fallback model
--------------------------
Copilot's CLI offers no "cheap/haiku-tier" model.  ``claude-sonnet-4`` is the
oldest/smallest model in the roster and is used for the lightweight extraction
fallback call.
"""
from __future__ import annotations

import json
import shlex

from godel._run import run  # noqa: F401 — re-exported so tests can patch `godel.agents._copilot.run`
from godel.agents._common import _BaseAgent

__all__ = ["copilot"]

# Copilot model identifiers.  "default" resolves to copilot's own default
# (currently gpt-5 per CLI docs).
_MODEL_ALIASES: dict[str, str] = {
    "default": "gpt-5",
    "gpt-5": "gpt-5",
    "sonnet": "claude-sonnet-4.5",
    "sonnet-4": "claude-sonnet-4",
    "claude-sonnet-4.5": "claude-sonnet-4.5",
    "claude-sonnet-4": "claude-sonnet-4",
}

# Cheapest available model for the extraction-fallback call.
_EXTRACTION_MODEL = "claude-sonnet-4"


class _CopilotAgent(_BaseAgent):
    _model_aliases = _MODEL_ALIASES
    _extraction_model = _EXTRACTION_MODEL

    def _build_command(
        self,
        prompt: str,
        model_id: str,
        *,
        tools: list[str] | None,
        session_id: str | None,
    ) -> str:
        cmd_parts = [
            "copilot",
            "--no-color",
            "--output-format", "json",
            "--model", model_id,
        ]
        if self._skip_permissions:
            cmd_parts.append("--allow-all-tools")
        if session_id:
            cmd_parts.append(f"--resume={shlex.quote(session_id)}")
        if tools:
            for tool in tools:
                cmd_parts += ["--allow-tool", shlex.quote(tool)]
        cmd_parts += ["-p", shlex.quote(prompt)]
        return " ".join(cmd_parts)

    def _parse_output(self, stdout: str) -> tuple[str, str | None]:
        """Parse copilot's JSONL event stream.

        Concatenates the content of all non-ephemeral ``assistant.message``
        events and reads ``sessionId`` from the terminating ``result`` event.
        If the stream doesn't look like JSONL at all (e.g. test fixtures or
        an older copilot build), fall back to returning the raw stripped text.
        """
        parts: list[str] = []
        session_id: str | None = None
        saw_event = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype is None:
                continue
            saw_event = True
            if etype == "assistant.message" and not event.get("ephemeral"):
                content = (event.get("data") or {}).get("content", "")
                if content:
                    parts.append(content)
            elif etype == "result":
                session_id = event.get("sessionId")

        if not saw_event or (not parts and session_id is None):
            return stdout.strip(), None
        return "\n".join(parts), session_id


def copilot(
    *,
    model: str = "default",
    cwd: str | None = None,
    tools: list[str] | None = None,
    skip_permissions: bool = False,
) -> _CopilotAgent:
    """Return an async callable that dispatches prompts to the Copilot CLI.

    Parameters
    ----------
    model:
        Model alias or full Copilot model identifier.  Recognised aliases:
        ``"default"`` (→ gpt-5), ``"gpt-5"``, ``"sonnet"`` (→ claude-sonnet-4.5),
        ``"sonnet-4"`` (→ claude-sonnet-4).
    cwd:
        Working directory passed to run().
    tools:
        Specific tool names to allow (forwarded as ``--allow-tool TOOL``).
        If *None*, no ``--allow-tool`` flags are added; combine with
        ``skip_permissions=True`` for unrestricted tool access.
    skip_permissions:
        When *True* pass ``--allow-all-tools`` to the Copilot CLI, mirroring
        ``--dangerously-skip-permissions`` in the Claude agent.
    """
    return _CopilotAgent(model=model, cwd=cwd, tools=tools, skip_permissions=skip_permissions)

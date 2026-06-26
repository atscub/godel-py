"""copilot agent factory.

copilot() returns an async callable that wraps the ``copilot`` CLI via run().
Every agent call goes through run() — the single audited escape hatch.

CLI choice
----------
The ``copilot`` binary comes from the ``@github/copilot-cli`` npm package
(GitHub Copilot CLI 1.0.25+).  It supports a genuine non-interactive path::

    copilot -p PROMPT --model MODEL --allow-all-tools --no-color \\
            --output-format json [--resume SESSION_ID]

Session persistence
-------------------
When ``--output-format json`` is set, the CLI streams one JSON object per line
and terminates with a ``{"type":"result","sessionId":...}`` event.  We capture
``sessionId`` from that event and pass ``--resume <id>`` on subsequent calls so
the conversation history is preserved.

Extraction fallback model
--------------------------
Copilot's CLI offers no "cheap/haiku-tier" model.  ``claude-sonnet-4`` is the
oldest/smallest model in the roster and is used for the lightweight extraction
fallback call.
"""
from __future__ import annotations

import json

from godel._run import run  # noqa: F401 — re-exported for backward-compat; canonical patch target is godel.agents._common.run
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
        streaming: bool = False,
    ) -> list[str]:
        # Copilot already emits JSONL (one object per line) regardless of the
        # streaming flag; no extra CLI flag is required.  The streaming
        # parameter is accepted for API compatibility with _BaseAgent but is
        # otherwise unused here.
        cmd_parts = [
            "copilot",
            "--no-color",
            "--output-format", "json",
            "--model", model_id,
        ]
        if self._skip_permissions:
            cmd_parts.append("--allow-all-tools")
        if session_id:
            cmd_parts += ["--resume", session_id]
        if tools:
            for tool in tools:
                cmd_parts += ["--allow-tool", tool]
        cmd_parts += ["-p", prompt]
        return cmd_parts

    def _make_adapter(self):
        from godel.agents._adapters import CopilotAdapter
        return CopilotAdapter()

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
    system_prompt: str | None = None,
    session_id: str | None = None,
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
    system_prompt:
        Optional briefing text prepended to the *first* prompt sent to this
        agent instance.  Subsequent calls on the same instance do not repeat
        it, so the context already lives in the conversation session.

        Empty / whitespace-only strings are treated as "no system prompt"
        and ignored silently.

        The flag that records "system prompt already delivered" is flipped
        only after a successful CLI call, so a first-call failure leaves the
        briefing available for retry.

        Example::

            agent = copilot(
                system_prompt="You are the QA engineer. Always verify tests pass."
            )
            await agent("check feature A")   # preamble already in context
            await agent("check feature B")   # no repetition

        Resume behaviour:
            When a workflow resumes from an event log, agent objects are
            re-constructed with ``_system_prompt_sent=False`` but the
            session id is restored from the replayed events.  The runtime
            detects an existing session id and skips re-prepending, so the
            briefing is delivered exactly once even across a pause/resume.
    session_id:
        Resume a prior CLI session across process boundaries.  When supplied,
        the first call will pass ``--resume <session_id>`` to the Copilot CLI
        so the conversation history is preserved without a full workflow
        replay.

        The ``system_prompt`` (if any) is assumed to have been delivered in
        that prior session, so it will *not* be re-prepended.

        Empty / whitespace-only strings are treated as *no session* and
        normalised to ``None``.

        Use ``agent.session_id`` to retrieve the current id (useful for
        persisting across process restarts)::

            # --- process A ---
            agent = copilot(system_prompt="You are the QA engineer.")
            await agent("check feature A")
            sid = agent.session_id          # persist this string

            # --- process B ---
            agent = copilot(session_id=sid)
            await agent("check feature B")   # continues same session

        Precedence:
            Workflow replay always takes precedence over the ctor-supplied
            value.  When a ``@workflow`` replays from its event log it
            overwrites ``_session_id`` with the value stored in the log,
            so deterministic replay is preserved regardless of what was
            passed here.
    """
    return _CopilotAgent(
        model=model,
        cwd=cwd,
        tools=tools,
        skip_permissions=skip_permissions,
        system_prompt=system_prompt,
        session_id=session_id,
    )

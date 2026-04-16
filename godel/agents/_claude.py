"""claude_code agent factory.

claude_code() returns an async callable that wraps the `claude` CLI via run().
Every agent call goes through run() — the single audited escape hatch.

Session persistence
-------------------
When the same agent instance is called multiple times, subsequent calls resume
the conversation by passing ``--resume <session_id>``.  The session id is read
from the ``session_id`` field of claude's ``--output-format json`` response on
the first call.
"""
from __future__ import annotations

import json
import shlex

from godel._run import run  # noqa: F401 — re-exported for backward-compat; canonical patch target is godel.agents._common.run
from godel.agents._common import SchemaValidationFailure, _BaseAgent

__all__ = ["claude_code", "SchemaValidationFailure"]

_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

_EXTRACTION_MODEL = _MODEL_ALIASES["haiku"]


class _ClaudeCodeAgent(_BaseAgent):
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
    ) -> str:
        # Use stream-json when streaming is active so each event arrives as a
        # separate JSONL line that the adapter can classify.  Fall back to the
        # regular json format otherwise (preserves pre-change output shape).
        output_format = "stream-json" if streaming else "json"
        cmd_parts = ["claude", "--output-format", output_format]
        if streaming:
            cmd_parts.append("--verbose")
            # Emit content_block_delta events so thinking + response tokens
            # stream in real time instead of arriving as one batched
            # `assistant` event at the end of the turn.
            cmd_parts.append("--include-partial-messages")
        if self._skip_permissions:
            cmd_parts.append("--dangerously-skip-permissions")
        if tools == []:
            cmd_parts += ["--tools", '""']
        if session_id:
            cmd_parts += ["--resume", session_id]
        cmd_parts += ["-p", shlex.quote(prompt), "--model", model_id]
        if tools:
            for tool in tools:
                cmd_parts += ["--allowedTools", shlex.quote(tool)]
        return " ".join(cmd_parts)

    def _parse_output(self, stdout: str) -> tuple[str, str | None]:
        # Handle both regular json (single object) and stream-json (JSONL).
        # For stream-json, we extract "result" text from the final "result" event
        # and the session_id from that same event.
        lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
        if not lines:
            return stdout.strip(), None
        # Try multi-line (stream-json): look for a terminating "result" event.
        if len(lines) > 1:
            for line in reversed(lines):
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and data.get("type") == "result":
                        return data.get("result", ""), data.get("session_id")
                except json.JSONDecodeError:
                    continue
            # No result event found — fall through to single-object attempt.
        # Try single-object json (non-streaming mode).
        try:
            data = json.loads(stdout)
            return data.get("result", stdout), data.get("session_id")
        except json.JSONDecodeError:
            return stdout.strip(), None

    def _make_adapter(self):
        from godel.agents._adapters import ClaudeAdapter
        return ClaudeAdapter()


def claude_code(
    *,
    model: str = "sonnet",
    cwd: str | None = None,
    tools: list[str] | None = None,
    skip_permissions: bool = False,
    system_prompt: str | None = None,
) -> _ClaudeCodeAgent:
    """Return an async callable that dispatches prompts to the Claude CLI.

    Parameters
    ----------
    model:
        Model alias or full Claude model identifier.  Recognised aliases:
        ``"sonnet"`` (→ claude-sonnet-4-6), ``"opus"`` (→ claude-opus-4-6),
        ``"haiku"`` (→ claude-haiku-4-5-20251001).
    cwd:
        Working directory passed to run().
    tools:
        Specific tool names to allow (forwarded as ``--allowedTools TOOL``).
        If *None*, no ``--allowedTools`` flags are added.
    skip_permissions:
        When *True* pass ``--dangerously-skip-permissions`` to the Claude CLI.
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

            eng = claude_code(
                system_prompt="You are the engineer for ticket X. Always run pytest before commit."
            )
            await eng("implement feature A")   # preamble already in context
            await eng("now implement feature B")  # no repetition

        Resume behaviour:
            When a workflow resumes from an event log, agent objects are
            re-constructed with ``_system_prompt_sent=False`` but the
            session id is restored from the replayed events.  The runtime
            detects an existing session id and skips re-prepending, so the
            briefing is delivered exactly once even across a pause/resume.
    """
    return _ClaudeCodeAgent(
        model=model,
        cwd=cwd,
        tools=tools,
        skip_permissions=skip_permissions,
        system_prompt=system_prompt,
    )

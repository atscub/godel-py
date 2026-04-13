"""Default Godel-shipped intervention agent.

The agent is itself a ``@workflow`` so every intervention session produces its
own audit log at ``runs/<intervention_run_id>.jsonl``, a sibling of the
original run's log.  This satisfies the M7 requirement that the intervention
agent is itself auditable and replayable.

Usage (programmatic)::

    from godel.intervention.default_agent import default_intervention_agent
    from godel.intervention import build_intervention_context, InterventionToolset

    ctx   = build_intervention_context(run_id, runs_dir=runs_dir)
    tools = InterventionToolset(ctx, runs_dir=runs_dir)
    outcome = await default_intervention_agent(ctx, tools)

The ``godel repair`` CLI resolves this function by default and passes
``--agent module:function_name`` to substitute a custom agent.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from godel._decorators import step, workflow
from godel.agents._claude import claude_code
from godel.intervention._context import InterventionContext
from godel.intervention._tools import (
    EditFileArgs,
    GiveUpArgs,
    InputArgs,
    InterventionToolset,
    ReadFileArgs,
    ResumeArgs,
    RewindArgs,
    GaveUp,
    ResumeRequested,
    tool_specs,
)

# ---------------------------------------------------------------------------
# Prompt sanitisation helpers
# ---------------------------------------------------------------------------


def _escape_backticks(text: str) -> str:
    """Escape triple-backtick sequences to prevent prompt injection via fenced blocks.

    Replaces ``` with a zero-width-space-separated form that renders visually
    identical in most contexts but does not close the enclosing markdown fence.
    """
    return text.replace("```", "`\u200b`\u200b`")


# ---------------------------------------------------------------------------
# Tool-call schema — one structured object per agent turn
# ---------------------------------------------------------------------------


class _ToolCall(BaseModel):
    """Single tool call produced by the LLM on each iteration."""

    tool: Literal["rewind", "resume", "input", "give_up", "read_file", "edit_file"]
    args: dict
    rationale: str = ""


# ---------------------------------------------------------------------------
# Argument models registry — maps tool name → pydantic model for args
# ---------------------------------------------------------------------------

_ARG_MODELS: dict[str, type[BaseModel]] = {
    "rewind": RewindArgs,
    "resume": ResumeArgs,
    "input": InputArgs,
    "give_up": GiveUpArgs,
    "read_file": ReadFileArgs,
    "edit_file": EditFileArgs,
}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def _dispatch(tools: InterventionToolset, name: str, raw_args: dict):
    """Validate *raw_args* against the registered model and invoke the tool.

    Raises:
        KeyError:  if *name* is not in _ARG_MODELS (unknown tool).
        ValueError: (reformatted from ValidationError) if raw_args don't satisfy
            the arg model.  The error message is formatted as ``field: msg``
            lines so the repair agent receives actionable structured feedback
            rather than a raw pydantic traceback blob (W2).
        ResumeRequested: if the ``resume`` tool is called.
        GaveUp:          if the ``give_up`` tool is called.
    """
    model = _ARG_MODELS[name]
    try:
        args = model.model_validate(raw_args)
    except ValidationError as exc:
        # W2: format the ValidationError into concise ``field: msg`` lines so
        # the repair agent gets actionable information rather than a multi-line
        # pydantic traceback blob that degrades repair quality.
        lines = [f"{' -> '.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ValueError(f"tool args validation failed for {name!r}:\n" + "\n".join(lines)) from exc
    return await getattr(tools, name)(args)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_STRATEGY_HINTS = """\
Strategy hints:
- If the failure is a typo or schema mismatch in the workflow source: use
  edit_file to patch the source, then rewind to the failed step, then resume.
- If the workflow is PAUSED at an input() call: use input to inject a value,
  then resume.
- If you cannot identify a defensible fix after reading the sources and events:
  call give_up with a clear reason.
- Never call resume without first ensuring the workflow is in a state that can
  succeed on re-execution."""


def _compact_event(ev) -> dict:
    """Return a compact, human-readable representation of an Event."""
    d: dict = {
        "id": ev.event_id,
        "op": ev.op,
        "status": ev.status.value,
        "step_path": list(ev.step_path),
    }
    if ev.ts_start:
        d["ts_start"] = ev.ts_start
    if ev.ts_end:
        d["ts_end"] = ev.ts_end
    # Include a short excerpt of request/response if present
    if ev.request:
        excerpt = {k: v for k, v in ev.request.items() if k not in ("source_file", "source_hash")}
        d["request_excerpt"] = excerpt
    if ev.response:
        d["response_excerpt"] = ev.response
    return d


def _build_prompt(
    ctx: InterventionContext,
    tools: InterventionToolset,
    transcript: list[dict],
    *,
    iteration: int,
) -> str:
    """Build the full prompt for one iteration of the agent loop.

    Includes:
    - Situation header (run_id, state, failure or paused)
    - Workflow source files (fenced)
    - Last 20 events (compact JSON)
    - Local state snapshot
    - Tool specifications
    - Transcript so far
    - Strategy hints
    - Strict instruction to reply with only a _ToolCall JSON object
    """
    parts: list[str] = []

    # ── Situation ──────────────────────────────────────────────────────────
    parts.append(f"# Godel Repair — Iteration {iteration + 1}")
    parts.append(f"\n**Run ID**: {ctx.run_id}")
    parts.append(f"**Run state**: {ctx.run_state}")

    if ctx.failure:
        f = ctx.failure
        # Sanitize error strings: they may contain markdown / backtick sequences
        # that could inject instructions into the repair agent prompt.
        safe_error = _escape_backticks(str(f.error))
        safe_error_type = _escape_backticks(str(f.error_type))
        parts.append(
            f"**Failure**: {safe_error_type}: {safe_error}\n"
            f"  op={f.op!r}  step_path={f.step_path!r}\n"
            f"  source_location={f.source_location!r}\n"
            f"  remediation_hint={f.remediation_hint!r}"
        )
    elif ctx.run_state == "PAUSED":
        parts.append(
            f"**Paused at input prompt**: {ctx.paused_input_prompt!r}"
        )
    else:
        parts.append("**No terminal failure recorded — run may be paused or still running.**")

    # ── Workflow source files ───────────────────────────────────────────────
    if ctx.sources:
        parts.append("\n## Workflow Sources")
        for src in ctx.sources:
            parts.append(f"\n### {src.path}  (sha256: {src.sha256[:16]}…)")
            # Escape backtick sequences in source content to prevent prompt
            # injection: a crafted source file could break out of the fence
            # and inject instructions to the repair agent (C3).
            safe_content = _escape_backticks(src.content)
            parts.append(f"```python\n{safe_content}\n```")
    else:
        parts.append("\n## Workflow Sources\n*(no sources available)*")

    # ── Recent events ──────────────────────────────────────────────────────
    recent = ctx.events[-20:]
    parts.append("\n## Recent Events (last 20)")
    parts.append(json.dumps([_compact_event(e) for e in recent], indent=2))

    # ── Local state ────────────────────────────────────────────────────────
    parts.append("\n## Local State Snapshot")
    parts.append(json.dumps(ctx.local_state, indent=2))

    # ── Tool specs ─────────────────────────────────────────────────────────
    parts.append("\n## Available Tools")
    parts.append(json.dumps(tool_specs(), indent=2))

    # ── Transcript so far ──────────────────────────────────────────────────
    if transcript:
        parts.append("\n## Transcript So Far")
        parts.append(json.dumps(transcript, indent=2))
    else:
        parts.append("\n## Transcript So Far\n*(first iteration — no prior actions)*")

    # ── Strategy hints ─────────────────────────────────────────────────────
    # W4: _STRATEGY_HINTS is multiline — must NOT be embedded in the heading
    # line itself, otherwise the body lines are orphaned from the ## heading.
    parts.append("\n## Strategy Hints")
    parts.append(_STRATEGY_HINTS)

    # ── Strict instruction ─────────────────────────────────────────────────
    parts.append(
        "\n## Instruction\n"
        "Analyse the situation above and decide on ONE tool call.\n"
        "Reply with ONLY a JSON object — no markdown fences, no explanation — "
        "matching this schema:\n"
        "```\n"
        '{"tool": "<name>", "args": {<tool args>}, "rationale": "<brief reason>"}\n'
        "```\n"
        "Tool names: rewind, resume, input, give_up, read_file, edit_file"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Default intervention workflow
# ---------------------------------------------------------------------------


def _make_intervention_workflow(
    ctx: InterventionContext,
    tools: InterventionToolset,
    *,
    model: str = "opus",
    max_iterations: int = 8,
):
    """Factory that returns a ``@workflow``-decorated coroutine whose positional
    signature is ``(run_id: str, run_state: str)`` — slim, serialisable values.

    ``ctx`` and ``tools`` are captured via closure so the ``@workflow``
    WORKFLOW_STARTED audit event records only ``repr(("run_id_val", "FAILED"))``
    rather than a full repr of InterventionContext (which can be megabytes of
    events + source contents).  This addresses C2.
    """

    @workflow
    async def _impl(run_id: str, run_state: str) -> dict:
        """Inner intervention agent workflow (slim-arg form for audit log).

        Loops up to *max_iterations* times:
        1. Build a prompt from the intervention context, tools, and transcript.
        2. Call ``claude_code`` with ``schema=_ToolCall`` to get a structured tool call.
        3. Dispatch the tool call.
        4. Append call + result to the transcript.
        5. Repeat until ``ResumeRequested`` or ``GaveUp`` is raised, or the
           iteration budget is exhausted.

        Returns:
            ``{"outcome": "resume"|"give_up", "reason": str, "iterations": N}``
        """
        transcript: list[dict] = []
        outcome: dict | None = None

        for i in range(max_iterations):

            # ── reason_and_call step ──────────────────────────────────────────
            # C4: name each iteration's step uniquely so the audit log is
            # unambiguous.  invocation_seq also disambiguates, but explicit names
            # make log queries much simpler.
            prompt = _build_prompt(ctx, tools, transcript, iteration=i)
            step_name = f"reason_and_call_{i}"

            @step(name=step_name)
            async def reason_and_call(_prompt=prompt):
                agent = claude_code(model=model, skip_permissions=True)
                return await agent(_prompt, schema=_ToolCall)

            # C1: wrap the LLM call so that SchemaValidationFailure (malformed
            # LLM output) or any other exception from claude_code does NOT
            # escape the loop and crash the whole intervention workflow.
            # Instead, append an error observation to the transcript and let
            # the agent try again on the next iteration.
            try:
                call: _ToolCall = await reason_and_call()
            except Exception as llm_exc:
                transcript.append(
                    {
                        "iteration": i,
                        "error": str(llm_exc),
                        "type": type(llm_exc).__name__,
                        "phase": "reason_and_call",
                    }
                )
                # Feed the error back as an observation and continue the loop
                continue

            transcript.append(
                {"iteration": i, "tool": call.tool, "args": call.args, "rationale": call.rationale}
            )

            # ── dispatch step ─────────────────────────────────────────────────
            try:
                result = await _dispatch(tools, call.tool, call.args)
                # Serialize result if it's a pydantic model
                result_value = result.model_dump() if hasattr(result, "model_dump") else result
                transcript.append({"iteration": i, "result": result_value})

            except ResumeRequested as r:
                outcome = {"outcome": "resume", "reason": r.reason, "iterations": i + 1}
                break

            except GaveUp as g:
                outcome = {"outcome": "give_up", "reason": g.reason, "iterations": i + 1}
                break

            except Exception as exc:
                transcript.append(
                    {
                        "iteration": i,
                        "error": str(exc),
                        "type": type(exc).__name__,
                    }
                )
                # Do not break — feed error back to agent as an observation

        if outcome is None:
            outcome = {
                "outcome": "give_up",
                "reason": f"max_iterations ({max_iterations}) exceeded without resolution",
                "iterations": max_iterations,
            }

        return outcome

    return _impl


async def default_intervention_agent(
    ctx: InterventionContext,
    tools: InterventionToolset,
    *,
    model: str = "opus",
    max_iterations: int = 8,
) -> dict:
    """Default Godel-shipped intervention agent.

    Creates a ``@workflow``-decorated coroutine whose positional signature is
    ``(run_id: str, run_state: str)`` so the WORKFLOW_STARTED audit event
    captures a slim summary instead of a full repr of the InterventionContext
    (which can contain all events and source file contents).  C2.

    Loops up to *max_iterations* times, reasoning and dispatching tool calls
    until ``ResumeRequested`` or ``GaveUp`` is raised, or the budget runs out.

    Returns:
        ``{"outcome": "resume"|"give_up", "reason": str, "iterations": N}``
    """
    impl = _make_intervention_workflow(ctx, tools, model=model, max_iterations=max_iterations)
    return await impl(ctx.run_id, ctx.run_state)

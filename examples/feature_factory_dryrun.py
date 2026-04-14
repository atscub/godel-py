"""Dry-run harness for feature_factory.

Replaces `claude_code` with a fake agent that returns canned schema-valid
responses, and auto-answers `input()` checkpoints. Exercises the full
control flow (loops, parallel, max-round caps) without hitting the
network, git, or beads.

Run:
    python -m godel run examples/feature_factory_dryrun.py
"""
from __future__ import annotations

import itertools
from typing import Any

import godel.io
from godel import workflow, print as gprint

# --- fake agent -------------------------------------------------------------

_counters: dict[str, itertools.count] = {}


def _next(key: str) -> int:
    c = _counters.setdefault(key, itertools.count())
    return next(c)


def _fake_response(prompt: str, schema: Any | None):
    # text-only response (no schema) — architect draft, deliver summary, etc.
    if schema is None:
        if "Tidy up" in prompt:
            return "Built fake feature X. Branch merged, ticket closed."
        return "ok"
    name = schema.__name__
    n = _next(name)
    if name == "IdeaBatch":
        return schema(ideas=[
            {
                "title": "Add --json flag to `godel workflows`",
                "problem": "Scripts can't consume workflow list programmatically.",
                "solution": "Emit JSON array when --json is passed.",
                "acceptance_criteria": [
                    "Running `godel workflows --json` prints valid JSON.",
                    "Output is an array of {name, path}.",
                    "Existing human output unchanged without flag.",
                ],
                "agent_testable": True,
                "single_task_scope": True,
            },
            {
                "title": "Rewrite entire event log to SQLite",
                "problem": "JSONL is slow.",
                "solution": "SQLite backend.",
                "acceptance_criteria": ["All tests pass"],
                "agent_testable": True,
                "single_task_scope": False,
            },
        ])
    if name == "PMVerdict":
        # first round: all high-risk (loop back); second round: pick 0
        if n == 0:
            return schema(
                chosen_index=None,
                assessments=[
                    {"idea_index": 0, "risk_level": "high", "rationale": "fake: retry"},
                    {"idea_index": 1, "risk_level": "high", "rationale": "scope too big"},
                ],
                reason="all high risk, re-brainstorm",
            )
        return schema(
            chosen_index=0,
            assessments=[
                {"idea_index": 0, "risk_level": "low", "rationale": "isolated flag"},
                {"idea_index": 1, "risk_level": "high", "rationale": "scope"},
            ],
            reason="idea 0 is low risk",
        )
    if name == "TicketRef":
        return schema(id="bd-fake-001")
    if name == "PlanReview":
        # first: reject; second: approve
        if n == 0:
            return schema(
                approved=False,
                coherence_issues=["plan misses JSON schema detail"],
                technical_risks=["no test for empty list"],
                required_changes=["add schema doc + empty-list test"],
            )
        return schema(approved=True, coherence_issues=[], technical_risks=[], required_changes=[])
    if name == "ImplementResult":
        return schema(
            branch="feat/bd-fake-001",
            commit_sha="deadbeef",
            files_changed=["godel/cli.py", "tests/test_cli_json.py"],
            summary="added --json flag",
        )
    if name == "AcceptanceReport":
        # first round: fail; second: pass
        if n == 0:
            return schema(
                passed=False,
                failures=["--json output not array when zero workflows"],
                evidence=["ran in empty project"],
            )
        return schema(passed=True, failures=[], evidence=["all 3 criteria probed"])
    if name == "CodeReviewReport":
        if n == 0:
            return schema(
                approved=False,
                blocking_issues=["missing type hint on new helper"],
                nits=["rename var"],
            )
        return schema(approved=True, blocking_issues=[], nits=["docstring nit"])
    raise RuntimeError(f"no fake for schema {name}")


class _FakeAgent:
    def __init__(self, model: str):
        self._model = model

    async def __call__(self, prompt: str, *, schema=None):
        head = prompt.splitlines()[0][:80]
        await gprint(f"  [fake:{self._model}] {head!r} schema={schema.__name__ if schema else None}")
        return _fake_response(prompt, schema)


def _fake_claude_code(*, model: str = "sonnet", **_: Any) -> _FakeAgent:
    return _FakeAgent(model)


# --- monkeypatch before workflow imports agents -----------------------------

import godel.agents as _agents_mod
_agents_mod.claude_code = _fake_claude_code  # type: ignore[assignment]


# auto-answer checkpoints
async def _auto_input(prompt: str = "") -> str:
    await gprint(f"  [checkpoint auto-ok] {prompt}")
    return ""


import godel as _godel_mod
godel.io.input = _auto_input  # type: ignore[assignment]
_godel_mod.input = _auto_input  # type: ignore[assignment]

@workflow
async def feature_factory_dryrun():
    # lazy import so godel's file scanner sees only this workflow
    from examples.feature_factory import feature_factory as _real
    # the @workflow-decorated callable wraps the coroutine fn; call .__wrapped__
    # if present, else the raw underlying function via its closure.
    inner = getattr(_real, "__wrapped__", _real)
    result = await inner()
    await gprint(f"[dryrun] completed: {result}")
    return result

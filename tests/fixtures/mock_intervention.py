"""Deterministic test-mode intervention workflow for repair E2E test.

Exposes ``intervene(ctx, tools, ...)`` which the CLI calls.  Internally it
builds a ``@workflow``-decorated closure (slim args: ``run_id: str``) so the
intervention session is fully audited — every edit and resume call appears in
the event log.  This matches the pattern in ``godel/intervention/default_agent.py``.

This module is injected into sys.path via PYTHONPATH so that
``godel repair --agent mock_intervention:intervene`` can import it.
No model calls, no randomness.
"""
from godel._decorators import workflow
from godel.intervention._tools import EditFileArgs, ResumeArgs


def _make_intervention_workflow(ctx, tools):
    """Factory: returns a ``@workflow``-decorated coroutine with a slim signature.

    ``ctx`` and ``tools`` are captured via closure so the WORKFLOW_STARTED
    audit event records only the run_id (a small string) rather than a full
    repr of the InterventionContext.
    """

    @workflow
    async def _impl(run_id: str) -> dict:
        """Inner intervention workflow — fix the schema-mismatch typo and resume."""
        assert len(ctx.sources) >= 1, (
            f"Expected at least one source in ctx.sources, got {len(ctx.sources)}"
        )
        source_path = ctx.sources[0].path

        # Fix the schema-mismatch typo: Count(count="one") -> Count(count=1).
        # NIT-1: anchor on the function-signature line (comment-free) so the
        # anchor is not broken if the trailing comment text changes.
        await tools.edit_file(EditFileArgs(
            path=source_path,
            old_str='async def step_two(prev: str) -> Count:\n    return Count(count="one")  # REPAIR_TARGET',
            new_str="async def step_two(prev: str) -> Count:\n    return Count(count=1)  # REPAIR_TARGET",
        ))

        await tools.resume(ResumeArgs(reason="typo fixed: Count(count='one') -> Count(count=1)"))
        return {"outcome": "resume"}

    return _impl


async def intervene(ctx, tools, *, model="opus", max_iterations=8):
    """Fix the schema-mismatch typo and signal resume.

    This is the entry point the CLI calls.  It creates a ``@workflow``-decorated
    inner function (capturing ``ctx`` and ``tools`` via closure) so the
    intervention session is fully audited, then invokes it with a slim ``run_id``
    arg.  The ``_is_workflow = True`` marker satisfies the CLI's validation check
    on custom ``--agent`` callables.
    """
    impl = _make_intervention_workflow(ctx, tools)
    return await impl(ctx.run_id)


# Satisfy the ``godel repair --agent`` validation check: the CLI verifies that
# the callable has ``_is_workflow = True``.  The actual auditing is performed by
# the inner @workflow-decorated closure created by _make_intervention_workflow.
intervene._is_workflow = True

"""Fixture workflow for parallel rewind testing.

Usage:
    python -m godel run tests/fixtures/parallel_rewind_wf.py --no-strict
"""
from godel import workflow, step, det
from godel._decorators import parallel
from godel._run import run


@step
async def branch_a():
    t = det.now()
    result = await run("echo branch_a_done", idempotent=True)
    return {"time": str(t), "output": result.stdout.strip()}


@step
async def branch_b():
    t = det.now()
    result = await run("echo branch_b_done", idempotent=True)
    return {"time": str(t), "output": result.stdout.strip()}


@step
async def final_step(a_result, b_result):
    return f"a={a_result['output']}, b={b_result['output']}"


@workflow
async def parallel_rewind_test():
    a_result, b_result = await parallel(branch_a(), branch_b())
    result = await final_step(a_result, b_result)
    return result

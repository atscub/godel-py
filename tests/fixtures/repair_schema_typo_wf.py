"""Fixture workflow for repair-schema-mismatch E2E test.

The typo: step_two returns Count(count="one") where Count.count expects int.
The repair agent rewrites Count(count="one") -> Count(count=1).

step_one uses an idempotent run() so the test can assert it was NOT
re-executed after resume (exactly one FINISHED run event for 'step_one_ran').
"""
from pydantic import BaseModel

from godel import workflow, step, run


class Count(BaseModel):
    count: int


@step
async def step_one() -> str:
    r = await run("echo step_one_ran", idempotent=True)
    return r.stdout.strip()


@step
async def step_two(prev: str) -> Count:
    return Count(count="one")  # REPAIR_TARGET: "one" should be 1


@step
async def step_three(c: Count) -> str:
    return f"done:{c.count}"


@workflow
async def wf():
    a = await step_one()
    b = await step_two(a)
    return await step_three(b)

"""Fixture workflow for pause-edit-resume E2E test.

step_a is fast (cached after pause), step_slow blocks on a file barrier,
step_tail is mutated by the test before resume.

Environment variable GODEL_PAUSE_DIR must be set to a directory path.
The workflow writes ``<GODEL_PAUSE_DIR>/ready`` when step_slow starts,
then polls for ``<GODEL_PAUSE_DIR>/release`` before continuing.

Optional environment variable GODEL_BODY_COUNTER_FILE: if set, step_slow
atomically increments a counter in that file on every body invocation.
This lets the test assert that the body re-executes exactly once during
replay (proving that @step bodies always run, even for cached steps).
"""
import os
import time
from pathlib import Path

from godel import workflow, step, run


@step
async def step_a() -> str:
    r = await run("echo step_a_ran", idempotent=True)
    return r.stdout.strip()


@step
async def step_slow() -> str:
    # Increment per-invocation counter if requested by the test harness.
    # This counter is used to prove step bodies re-execute during replay
    # (the engine calls fn() unconditionally; only run() hits the cache).
    counter_file = os.environ.get("GODEL_BODY_COUNTER_FILE")
    if counter_file:
        p = Path(counter_file)
        current = int(p.read_text().strip()) if p.exists() else 0
        p.write_text(str(current + 1))

    d = Path(os.environ["GODEL_PAUSE_DIR"])
    (d / "ready").write_text("1")
    for _ in range(200):
        if (d / "release").exists():
            break
        time.sleep(0.1)
    return "slow_done"


@step
async def step_tail() -> str:
    return "ORIGINAL_TAIL"  # EDIT_TARGET


@workflow
async def wf():
    a = await step_a()
    s = await step_slow()
    t = await step_tail()
    return f"{a}|{s}|{t}"

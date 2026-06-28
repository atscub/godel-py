"""Demo: rewind --assume-idempotent bypassing the safety check.

Single step with a non-idempotent run() call. After running:

  # Step 1 — run the workflow
  godel run examples/rewind_idempotent_demo.py

  # Step 2 — find the step event ID
  godel show <run-id>
  # Look for: step.enter  read_only_check  FINISHED

  # Step 3 — rewind WITHOUT flag (should FAIL with exit 2)
  godel rewind <run-id> --to <step-event-id>

  # Step 4 — rewind WITH flag (should SUCCEED)
  godel rewind <run-id> --to <step-event-id> --assume-idempotent

  # Step 5 — resume to re-execute from the rewind point
  godel resume <run-id>
"""

from godel import workflow, step, print, run


@step
async def read_only_check():
    """Non-idempotent run() that is actually safe (read-only)."""
    result = await run("uname -a", idempotent=False)
    await print(f"System: {result.stdout.strip()}")
    return result.stdout.strip()


@workflow
async def rewind_demo():
    info = await read_only_check()
    await print(f"Done. Result: {info}")
    return info

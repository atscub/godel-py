"""Test: verify ContextOverflowError fires when an agent session overflows.

Sends repeated large prompts to a Haiku agent until the context window fills up.
Expects ContextOverflowError to be raised, then recovers with a fresh agent.

Usage:
    godel run examples/context_overflow_test.py
"""

from godel import workflow, step, print, ContextOverflowError
from godel.agents import claude_code

LARGE_PAYLOAD = "x" * 50_000


@step
async def overflow_agent():
    agent = claude_code(model="haiku", skip_permissions=True, tools=[])
    call_count = 0

    while True:
        call_count += 1
        await print(f"[call {call_count}] sending ~50k chars...")
        try:
            result = await agent(
                f"Reply with exactly one word: 'ok'. Ignore the padding below.\n\n{LARGE_PAYLOAD}",
                assume_idempotent=True,
            )
            await print(f"[call {call_count}] got: {result.strip()[:50]}")
        except ContextOverflowError as exc:
            await print(f"\n[call {call_count}] ContextOverflowError raised!")
            await print(f"  model: {exc.model}")
            await print(f"  session_id: {exc.session_id}")
            await print(f"  stderr snippet: {exc.stderr[:200]}")
            return {
                "calls_before_overflow": call_count,
                "model": exc.model,
                "session_id": exc.session_id,
            }


@workflow
async def context_overflow_test():
    await print("=== Context Overflow Test ===")
    await print("Sending large prompts to haiku until context overflows...\n")

    result = await overflow_agent()

    await print(f"\nTest passed: overflow after {result['calls_before_overflow']} calls")
    await print(f"Model: {result['model']}")
    return result

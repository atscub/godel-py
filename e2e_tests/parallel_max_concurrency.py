"""E2E: verify parallel(max_concurrency=N) throttles concurrent branches.

Fans out 8 branches with max_concurrency=2.  Each branch increments a
shared counter on entry and decrements on exit, tracking peak concurrency.
Asserts that at most 2 branches were active simultaneously.

Usage:
    godel run e2e_tests/parallel_max_concurrency.py
"""

from godel import workflow, step, parallel, print, det


_active = 0
_peak = 0


@step
async def throttled_branch(index: int):
    global _active, _peak
    _active += 1
    if _active > _peak:
        _peak = _active
    await det.sleep(0.05)
    _active -= 1
    return index


@workflow
async def max_concurrency_e2e():
    global _peak
    await print("=== parallel() max_concurrency E2E ===")

    results = await parallel(
        *[throttled_branch(i) for i in range(8)],
        max_concurrency=2,
    )

    assert len(results) == 8, f"Expected 8 results, got {len(results)}"
    assert results == tuple(range(8)), f"Result order wrong: {results}"
    assert _peak <= 2, f"Peak concurrency {_peak} > 2"

    await print(f"All 8 branches completed, peak concurrency = {_peak}")
    await print("PASS")
    return {"branches": len(results), "max_concurrency": 2, "peak": _peak}

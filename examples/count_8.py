from godel import workflow, step
from godel._run import run

@step
async def slow_step(n: int) -> str:
    # Shells out to bash — gets its own process group; Ctrl+C kills it cleanly.
    r = await run(f"for i in $(seq 1 {n}); do echo line-$i; sleep 1; done")
    return r.stdout

@workflow(stream_agents=True)
async def demo():
    out = await slow_step(8)
    return out

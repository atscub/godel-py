from godel import workflow, step, run, print
from godel.agents import copilot


@step
async def slow_step(n: int) -> str:
    r = await run(f"for i in $(seq {n} -1 1); do echo t-minus: $i; sleep 1; done")
    return r.stdout


@step
async def ask_topic(agent) -> str:
    return await agent("Pick an obscure animal. Reply with just its name, nothing else.")


@step
async def ask_haiku(agent, animal: str) -> str:
    return await agent(f"Now write a haiku about that {animal}. Haiku only, no preamble.")


@step
async def ask_critique(agent) -> str:
    return await agent("In one sentence, what's the weakest line of your haiku and why?")


@step
async def write_about_photosynthsis(agent):
    return await agent("Write 5 paragraphs on photosynthesis.")


@workflow
async def chat():
    agent2 = copilot(model="claude-sonnet-4.6", skip_permissions=True)
    photosynthesis = await write_about_photosynthsis(agent2)
    return {"photosynthesis": photosynthesis}

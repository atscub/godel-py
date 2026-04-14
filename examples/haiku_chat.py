from godel import workflow, step, run, print
from godel.agents import claude_code

@step
async def slow_step(n: int) -> str:
    # Shells out to bash — gets its own process group; Ctrl+C kills it cleanly.
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


@workflow(stream_agents=True)
async def chat():
    await print("Countdown ...")
    await slow_step(5)
    # Same agent instance → session persists across calls
    agent = claude_code(model="haiku", skip_permissions=True)
    animal = (await ask_topic(agent)).strip()
    haiku = await ask_haiku(agent, animal)
    critique = await ask_critique(agent)
    return {"animal": animal, "haiku": haiku, "critique": critique}
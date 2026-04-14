from godel import workflow, step, parallel, print
from godel.agents import claude_code


@step
async def ask_haiku(agent, subject: str) -> str:
    return await agent(f"Write a haiku about {subject}. Haiku only, no preamble.")


@workflow
async def chat():
    await print("Launching two agents in parallel ...")
    ocean = claude_code(model="haiku", skip_permissions=True)
    mountain = claude_code(model="haiku", skip_permissions=True)

    ocean_haiku, mountain_haiku = await parallel(
        ask_haiku(ocean, "the ocean"),
        ask_haiku(mountain, "a mountain"),
    )

    # await print("\n--- ocean ---\n" + ocean_haiku)
    # await print("\n--- mountain ---\n" + mountain_haiku)
    return {"ocean": ocean_haiku, "mountain": mountain_haiku}

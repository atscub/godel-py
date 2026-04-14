"""Multi-step conversation that exercises tool calls + intermediate thinking.

Run:
    godel run examples/research_chat.py --plain

The first step asks the agent to do real work on disk (creating a tmp file,
listing it, reading it back) so stream-json emits tool_use / tool_result
events.  The follow-ups keep the same session so you can see the agent
reason about the earlier result.
"""
from godel import workflow, step, print
from godel.agents import claude_code


@step
async def investigate(agent) -> str:
    return await agent(
        "Create a temporary file at /tmp/godel_demo.txt containing three "
        "lines: the numbers 1, 2, and 3 on separate lines.  Then count how "
        "many lines it has using a shell command.  Finally, report ONLY the "
        "line count as a single digit."
    )


@step
async def reflect(agent) -> str:
    return await agent(
        "Based on what you just did, explain in one short sentence what "
        "could have gone wrong and how you verified it didn't."
    )


@step
async def finalize(agent, count: str, reflection: str) -> str:
    return await agent(
        f"Summarize the session in exactly two lines: line 1 = the count "
        f"you reported ({count.strip()}), line 2 = a six-word recap of the "
        f"reflection."
    )


@workflow(stream_agents=True)
async def chat():
    await print("── research demo ──")
    agent = claude_code(model="haiku", skip_permissions=True)
    count = await investigate(agent)
    reflection = await reflect(agent)
    summary = await finalize(agent, count, reflection)
    return {"count": count.strip(), "reflection": reflection, "summary": summary}

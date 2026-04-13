# Why Godel

Agent workflows fail in predictable ways. They drift from the plan. They forget state. They skip human checkpoints. They re-derive solutions they already found. Every team building with AI agents reaches for one of the same three crutches — more prose, more code, or more agents — and none of them hold up.

## The core insight

Agent workflows have two fundamentally different parts, and conflating them is the root cause of most failures.

**Structural decisions** — what to do next, when to loop, where to branch, what state to track — are deterministic. They don't benefit from agent judgment. When an agent "decides" which step comes next by re-reading a prose document, it's doing unnecessary cognitive work on a solved problem, and occasionally getting it wrong.

**Operational decisions** — how to implement a fix, whether a review comment is valid, what the right test strategy is — require judgment. They can't be reduced to code. When a developer writes Python to orchestrate these steps, they're forced to pre-specify what should be left to the agent, and the result is brittle.

Godel draws the line. **Orchestration is plain, deterministic Python. Agents are leaves.** A `@workflow` function decides *what* happens next. The agents it calls decide *how*.

## What that buys you

Because the orchestrator is ordinary Python running under `godel.strict`, the runtime can be event-sourced: every agent call, subprocess, print, input, step boundary, and control-flow fork is appended to a log. That log is the audit trail, the replay tape, and the recovery surface, all at once.

Three primitives fall out of that design, and no other agent framework ships all three:

- **Resume** — a crashed or paused run picks up from the last durable event. Cached steps are not re-executed; expensive agent calls are not re-paid.
- **Rewind** — invalidate any event in the log and replay forward. The agent that went down the wrong path can back up and try again without throwing away the work that preceded it.
- **Hot-patch** — edit the `.py` file, then `godel resume`. The new code takes effect on the uncached tail. Combined with `rewind`, a completed step can be re-executed against corrected code.

Sitting on top of these is **intervention mode** — `godel pause` on a live run, `godel repair` on a crashed one — so another agent (or a human) can steer or recover using the same primitives through the same CLI.

## How it differs from the neighbours

- **Plain Python frameworks (LangGraph, CrewAI, AutoGen).** Pre-declared graphs or role-based patterns. You build logging yourself; you build checkpoint/resume yourself; rewind isn't a concept. Every branch and failure mode has to be anticipated at authoring time.
- **Durable-execution engines (Temporal, DBOS, Restate).** Resume, yes. Rewind, no. Agent-first primitives (closure-based session state, schema-coerced outputs, human-in-the-loop as an ordinary call), no. The Workflow/Activity split doesn't have a place to attach agent state to a call site.
- **Markdown runbooks & system prompts.** No runtime can execute them. State survives by luck; a heading isn't a procedure; a bullet isn't a step with error handling. Fine for simple linear tasks, unusable past ~30 minutes of agent work.
- **Task queues (Beads).** Complementary, not overlapping — Beads decides *what to work on next*; Godel defines *how to work on it*.

## When to reach for Godel

Reach for Godel when a workflow is too complex for a single prompt, too judgment-heavy for pure code, and too important to leave to prose:

- Multi-step processes where state must survive across phases (PR review loops, deployment pipelines, investigations).
- Workflows with human checkpoints that must not be skipped (approvals, security reviews, legal sign-offs).
- Processes you need to audit after the fact — "the agent was supposed to follow this procedure; did it?"
- Meta-orchestration where one agent authors the workflow and other agents execute it.

Don't reach for Godel when the task fits in a single prompt, or when there's no agent judgment in the loop at all (just write a script), or when you're building agent infrastructure rather than defining agent behaviour.

## The one-line version

Godel is what you write when the process is too complex for a prompt and too human for a program — and what an agent writes when it needs to orchestrate other agents.

# Godel — Python Library Documentation

Godel is a **deterministic orchestrator** for AI-agent workflows. You write a plain Python
`async` function, decorate it with `@workflow`, and call agents as ordinary `await`-able
callables. Godel records every non-deterministic event to an append-only audit log, so any
run can be inspected, paused, resumed, rewound, or repaired.

Think of it as *durable execution for LLM pipelines* — like Temporal or DBOS, but with
first-class support for agent calls, schema-validated outputs, and live human-in-the-loop
intervention.

## Contents

1. [Getting Started](getting-started.md) — install, write a workflow, run it
2. [Concepts](concepts.md) — workflows, steps, agents, the audit log, strict mode, replay
3. [API Reference](api-reference.md) — every public symbol exported from `godel`
4. [CLI Reference](cli.md) — the `godel` command-line tool
5. [Examples](examples.md) — annotated walkthroughs
6. [Agent Skills](skills/README.md) — `godel-runner` and `godel-engineer` for Claude Code

## Why Godel?

Most agent frameworks bury orchestration logic inside an LLM. That makes behavior hard to
predict, hard to debug, and hard to resume when something crashes three agents deep.

Godel inverts that: **the orchestrator is plain, deterministic Python**. Agents are leaves
in the call tree; the tree itself is code you can read, test, and step through. When a
workflow crashes, you can resume from the last durable event — no re-doing expensive
agent calls.

## Status

Pre-1.0. Core primitives (workflow/step/agent, audit log, strict mode, replay, pause,
rewind, repair) are implemented and covered by tests. API may shift before 1.0.

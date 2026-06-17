# Godel

**Deterministic orchestrator for AI agent workflows.** Orchestration is plain Python; agents are leaves. Every non-deterministic event is logged to an append-only audit trail that doubles as a replay tape — giving you resume, rewind, and hot-patch for free.

[![CI](https://github.com/atscub/godel-py/actions/workflows/publish.yml/badge.svg)](https://github.com/atscub/godel-py/actions/workflows/publish.yml)
[![PyPI](https://img.shields.io/pypi/v/godel-py)](https://pypi.org/project/godel-py/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/godel-py)](https://pypi.org/project/godel-py/)
[![Tests](https://img.shields.io/badge/tests-1276_passing-brightgreen)](#)

## The problem

Most agent frameworks bury orchestration inside the LLM. The agent decides what step comes next by re-reading a plan, occasionally getting it wrong. State survives by luck. When something crashes three agents deep, you start over.

## The insight

Agent workflows have two parts: **structural decisions** (what to do next, when to branch, what state to track) and **operational decisions** (how to implement a fix, whether a review is valid). The first is deterministic and doesn't need an LLM. The second requires judgment and can't be reduced to code.

Godel draws the line. `@workflow` functions handle structure. Agents handle judgment. Because the orchestrator is deterministic Python, the runtime can event-source every call — and three primitives fall out:

- **Resume** — a crashed run picks up from the last durable event. Expensive agent calls are not re-paid.
- **Rewind** — invalidate any event and replay forward. Back up without discarding prior work.
- **Hot-patch** — edit the workflow, then `godel resume`. New code takes effect on the uncached tail.

No other agent framework ships all three.

## Install

```bash
pip install godel-py
```

## Quick start

```python
from godel import workflow, step, run

@step
def review(pr_number):
    return run(["claude", "-p", f"Review PR #{pr_number} for correctness bugs"])

@step
def apply_fixes(review_output):
    return run(["claude", "-p", f"Apply these fixes: {review_output}"])

@workflow
def pr_pipeline(pr_number):
    findings = review(pr_number)
    if "no issues" not in findings.lower():
        apply_fixes(findings)
```

```bash
godel run pr_pipeline.py -- --pr-number 42
```

Crash at any point. `godel resume` picks up where it left off — no re-running the review.

## How it differs

| | Godel | LangGraph / CrewAI | Temporal / DBOS |
|---|---|---|---|
| Orchestration | Plain Python | Pre-declared graphs / roles | Workflow/Activity split |
| Resume from crash | Built-in (event log) | Build it yourself | Built-in |
| Rewind to any point | Built-in | Not a concept | Not available |
| Hot-patch live run | Built-in | Redeploy | Redeploy |
| Human-in-the-loop | `godel pause` / `godel repair` | Custom | Custom |
| Agent-first primitives | Session state, schema outputs | Varies | Not designed for agents |

## Architecture

```
@workflow function          # you write this — plain Python
  -> @step calls            # each step is an event boundary
    -> run() / agent()      # shells out to Claude, Copilot, or any CLI
      -> event log          # append-only, deterministic replay backbone
        -> resume / rewind / repair
```

The [language spec](https://github.com/atscub/godel-lang) defines the formal model. This repo is the Python runtime.

## Documentation

- [Why Godel](docs/why-godel.md) — the thesis in depth
- [Getting Started](docs/getting-started.md) — install, write a workflow, run it
- [Concepts](docs/concepts.md) — workflows, steps, agents, the audit log, replay
- [API Reference](docs/api-reference.md) — every public symbol
- [CLI Reference](docs/cli.md) — the `godel` command
- [Examples](examples/) — annotated walkthroughs

## Development

```bash
pip install -e ".[dev]"
pytest                      # 1276 tests across 98 files
```

Python 3.10+. Conventional commits. CI runs on every push to `main`.

## License

Business Source License 1.1 — see [LICENSE](LICENSE). Use Godel freely as a library in your applications, including commercially. The only restriction is offering Godel itself as a competing hosted orchestration service. Each version converts to Apache 2.0 after six years.

# Agent Skills

Two Claude Code skills ship with Godel. They are plain Markdown skill files — drop them
into `~/.claude/skills/` (or a project's `.claude/skills/`) and invoke them via the
`Skill` tool or `/skill-name` in Claude Code.

| Skill | Use it when... |
|-------|----------------|
| [`godel-runner`](godel-runner.md) | You want an agent to execute, monitor, and repair Godel workflows (run / resume / pause / rewind / repair). |
| [`godel-engineer`](godel-engineer.md) | You want an agent to **write** Godel workflows — decide when to use `@step`, wire up agents, design schemas, handle failures. |

Both skills assume the agent has shell access and that `godel` is on `PATH`.

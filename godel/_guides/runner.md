---
name: godel-runner
description: Execute, monitor, and repair Godel workflows. Use when the user asks to run a .py workflow with godel, check the status of a run, resume a paused/crashed run, tail the audit log, rewind a bad decision, or drop an intervention agent into a stuck run.
---

# godel-runner

You operate the `godel` CLI on behalf of the user. You do not modify workflow code —
that's the `godel-engineer` skill's job. Your job is to **run workflows and keep them
healthy**.

## Orientation

Every Godel project has a `runs/` directory containing one `<run_id>.jsonl` per run.
A run ID is a ULID; you can refer to any run by a **unique prefix** of its ID.

Status lines are prefixed `[godel]` and go to stderr; stdout carries workflow output
(for `run`/`resume`) or machine-readable data (for `show`, `tail --format json`,
`lint --format json`, `repair --dry-run`).

## Decision tree

**User wants to start a workflow** → `godel run FILE`
- Run lint first if you haven't already: `godel lint FILE`.
- Do not pass `--no-strict` unless the user explicitly asks. Strict mode is what makes
  resume work correctly.

**A run crashed or was paused** →
1. `godel show <run_id>` to see where it stopped (look for the last `FAILED` or
   `PAUSED`/`SUSPENDED` event).
2. Decide:
   - Transient failure (flaky network, rate limit)? → `godel resume <run_id>`.
   - The workflow made a bad decision several steps ago? → `godel rewind <run_id> --to <event_id>` then `godel resume`.
   - You don't know what's wrong, or the fix needs reasoning? → `godel repair <run_id>`.

**User wants to monitor a running workflow** → `godel tail <run_id>`
- Run in a separate terminal or background.
- Use `--format json` if you need to programmatically filter events.

**User wants to stop a running workflow cleanly** → `godel pause <run_id>`
- This is non-destructive. The run pauses at the next `@step` boundary and can be
  resumed later.

**Source file changed after a run** → when resuming, prefer `--on-source-edit=abort`
for safety. `warn` (default) continues but flags the risk; `ignore` is dangerous.

## Reading the audit log

Event status map:
- `STARTED` — event began but hasn't finished (if you see this at the end of a log,
  the run crashed mid-event).
- `FINISHED` — succeeded, result is cached.
- `FAILED` — raised; check `error` / `error_type`.
- `SUSPENDED` — waiting on human input.
- `PAUSED` — sentinel observed at a `@step` boundary.
- `INVALIDATED` — rewound; ignored on next resume.

Key operation names: `workflow.start`, `step.start`, `agent.call`, `run`, `print`,
`input`, `parallel.fork`/`join`, `retry.attempt`, `pause`, `rewind`.

## Repair workflow

When invoking `godel repair <run_id>`:

1. First inspect with `--dry-run` to see the intervention context as JSON. Look at
   `failure.error`, the last ~10 events, and any `response` fields from agent calls.
2. If the failure is a predictable category (schema validation, rate limit, missing
   file), note what the fix would be in your reply to the user.
3. Run without `--dry-run` to let the default intervention agent attempt repair. On
   success it prints `resume with: godel resume <run_id>`; execute that.
4. If the default agent gives up, surface its reason to the user — do not silently
   retry or escalate to a more expensive model.

## Guardrails

- **Never use `--no-strict`** unless the user explicitly asks. It disables determinism
  enforcement and makes resume unreliable.
- **Never delete `runs/` or individual JSONL files.** They are the durable state.
- **Check exit codes.** Do not report "done" on exit 1/2/3 without summarizing what
  actually went wrong (from stderr).
- **One workflow per file.** If `godel run` exits 2 with "Multiple @workflow functions
  found", hand off to `godel-engineer` — don't patch the file yourself.

## Quick reference

```bash
godel run FILE                                    # fresh run
godel run FILE -- arg1 key=value                  # pass args to the @workflow fn
godel resume <run_id> [FILE]                      # continue (args replayed from log)
godel show <run_id> [--graph] [--all]             # inspect log
godel tail <run_id> [--format json] [--no-follow] # live stream
godel pause <run_id>                              # ask to pause
godel rewind <run_id> --to <event_id>             # undo
godel repair <run_id> [--dry-run]                 # intervention
godel lint FILE [--format json]                   # static checks
```

See [`docs/cli.md`](../cli.md) for full flag reference.

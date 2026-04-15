# CLI Reference

The `godel` command is installed by `pip install -e .` (entry point:
`godel = "godel.cli:main"`). All commands expect to be run from a project root where
`./runs/` either exists or will be created.

## Output conventions

Status lines are prefixed with `[godel]` and go to **stderr**.

- `godel run` / `godel resume` produce **no stdout** of their own — anything on stdout
  is whatever the workflow itself printed.
- Data-emitting commands (`show`, `tail`, `lint --format json`, `repair --dry-run`)
  write their payload to **stdout**.

This split lets you pipe machine output while still seeing progress in the terminal:

```bash
godel tail 01JQ5Z --format json | jq 'select(.op == "agent.call")'
```

## Common flags

| Flag           | Meaning                                                             |
|----------------|---------------------------------------------------------------------|
| `--no-strict`  | Disable determinism enforcement (AST scan, import guard, audit hook). Also suppresses lint rule `PL003`. |
| `--no-lint`    | Skip the pre-flight lint check.                                     |

`RUN_ID` arguments accept any **unique prefix** of a run ID. Ambiguous prefixes exit
with the candidate list on stderr.

## Commands

### `godel run FILE [-- ARG ...]`
Execute the single `@workflow` function in `FILE`.

```bash
godel run examples/pr_review.py
```

`FILE` may be either a path to a `.py` file **or** the name of a workflow registered
under `<project>/.godel/workflows/` or `~/.godel/workflows/` (see
[`godel init`](#godel-init) and [`godel workflows`](#godel-workflows)). Path match wins;
otherwise name lookup runs and the resolved path is echoed on stderr.

| Flag            | Purpose                                                                     |
|-----------------|-----------------------------------------------------------------------------|
| `--watch`       | Spawn the live TUI renderer as a subprocess alongside the run (requires `godel[watch]`). |
| `--no-stream`   | Disable agent-response streaming for this run (streaming is on by default). Sets `GODEL_STREAM_AGENTS=0`. |
| `--plain`, `-p` | Force plain line-log output in the watcher (implies `--watch`; also honours `GODEL_WATCH_PLAIN=1`). |

**Passing arguments to the workflow.** Tokens after `--` are forwarded to the
`@workflow` function. Tokens containing `=` with a valid Python identifier LHS become
keyword args; all other tokens become positional args. All values are passed as strings —
the workflow is responsible for coercion.

```bash
godel run workflow.py -- alice bob                 # positional → fn("alice", "bob")
godel run workflow.py -- model=opus max_steps=10   # kwargs     → fn(model="opus", max_steps="10")
godel run workflow.py -- alice model=opus          # mixed
```

Edge cases: `q=a=b` splits on the first `=` (key `q`, value `a=b`); `x=` yields
`x=""`; `1=foo` (invalid identifier LHS) is treated as positional; duplicate kwarg keys
are rejected. Args are recorded in `WORKFLOW_STARTED`, so `godel resume` replays with the
same args automatically — do not re-supply them.

**Exit codes:** `0` success or clean pause · `1` lint error, `WorkflowFail`, or strict
violation · `2` other exception · `130` interrupt.

On pause, failure, or crash, Godel prints the resume command so you can continue later.

### `godel resume RUN_ID [FILE]`
Resume a paused or crashed run. `FILE` is recovered from the `WORKFLOW_STARTED` event
if omitted.

| Flag                                        | Purpose                                                       |
|---------------------------------------------|---------------------------------------------------------------|
| `--on-mismatch {continue|invalidate|abort}` | Policy when a cached operation's `request_hash` differs.      |
| `--on-source-edit {warn|abort|ignore}`      | Policy when a cached `@step`'s source was edited (default: `warn`). |
| `--no-strict`, `--no-lint`                  | Same semantics as `run`.                                      |
| `--no-stream`                               | Disable agent-response streaming on this resume.              |

### `godel show RUN_ID`
Render the audit log.

| Flag       | Purpose                                                    |
|------------|------------------------------------------------------------|
| `--graph`  | Render the DAG as an ASCII tree.                           |
| `--all`    | Include failed retries and invalidated (rewound) events.   |

### `godel tail RUN_ID`
Follow the audit log in real time.

| Flag                        | Purpose                                          |
|-----------------------------|--------------------------------------------------|
| `--format {pretty|json}`    | Colored text (default) or one JSON per line.     |
| `--no-follow`               | Exit at EOF instead of waiting for new events.   |
| `--no-wait`                 | Fail immediately if the log file doesn't exist yet. |

### `godel lint FILE`
Static checks. Rule IDs look like `PL001` … `PLNNN`.

| Flag                      | Purpose                                              |
|---------------------------|------------------------------------------------------|
| `--format {text|json}`    | Text (default) or JSON.                              |
| `--skip PL003,PL007`      | Skip specific rules (unknown IDs warn and are ignored). |

Exit `1` on errors; `0` on warnings-only or clean.

### `godel pause RUN_ID`
Request a **live** run to pause at its next `@step` boundary by writing
`runs/<run_id>.pause`. The running workflow notices the sentinel, raises `PauseSignal`,
and exits cleanly.

| Flag              | Purpose                                     |
|-------------------|---------------------------------------------|
| `--reason TEXT`   | Annotation recorded in the audit log.       |

### `godel rewind RUN_ID --to EVENT_ID[,EVENT_ID...]`
Invalidate one or more events and all their descendants so a subsequent `resume` will
re-execute from those points.

| Flag              | Purpose                                     |
|-------------------|---------------------------------------------|
| `--reason TEXT`   | Annotation recorded on the rewind event.    |

Exit `2` if the rewind would be unsafe (e.g. invalidating a subprocess that has no
recorded inverse).

### `godel repair RUN_ID`
Launch an **intervention agent** against a `PAUSED` or `FAILED` run. The agent can
inspect events, patch state, and propose resume.

| Flag                           | Purpose                                                       |
|--------------------------------|---------------------------------------------------------------|
| `--agent MODULE:FUNCTION`      | Custom intervention workflow (default: the built-in agent).   |
| `--model opus`                 | Model for the default intervention agent.                     |
| `--max-iterations 8`           | Cap the agent's reasoning loop.                               |
| `--dry-run`                    | Print the intervention context as JSON and exit.              |

Exit codes: `0` agent requested resume · `1` agent gave up · `2` bad state or bad
`--agent` · `3` agent crashed.

### `godel watch RUN_ID`
Attach the live Rich-TUI renderer to a running or completed run. Replays any archived
transcript, then follows the live transcript until the run ends or `Ctrl+C`. Requires
`godel[watch]`.

| Flag             | Purpose                                                              |
|------------------|----------------------------------------------------------------------|
| `--runs-dir DIR` | Override the transcript root (default: resolved from config).        |
| `--plain`, `-p`  | Plain line-log output instead of the TUI (also: `GODEL_WATCH_PLAIN=1`). |

If the target run was executed with `--no-stream`, `watch` prints a hint on stderr and
exits — there is no transcript directory to follow.

### `godel init`
Scaffold a `.godel/` directory (with `workflows/` subdir and a `settings.json` stub) in
the current project. Idempotent — existing files are reported as `exists, skipped`
rather than overwritten.

### `godel config path`
Print the config source chain in precedence order (user `~/.godel/settings.json` →
project `.godel/settings.json` → `GODEL_*` env → CLI flags) along with the effective
merged view and resolved `runs_dir`.

### `godel workflows`
Named-workflow registry. Workflows live in `.godel/workflows/*.py` (project) or
`~/.godel/workflows/*.py` (user); project entries shadow user entries on name collision.

- `godel workflows list` — list every discoverable workflow name → path.
- `godel workflows which NAME` — print the resolved path for `NAME` without running it
  (exit `1` if unresolved).

### `godel guide [NAME]`
Print one of the bundled onboarding guides shipped with the installed package, so agents
can pull just-in-time context without the godel repo being present. Available slugs:
`getting-started`, `concepts`, `engineer`, `runner`, `monitoring`, `cli`, `api-reference`. Call with
no `NAME` to list them all.

## Run-lifecycle cheat sheet

```
godel run workflow.py               # fresh run
godel run workflow.py --watch       # fresh run + live TUI
godel watch <run_id>                # attach TUI to a running/finished run
godel tail <run_id>                 # follow raw JSONL events
godel pause <run_id>                # ask it to pause cleanly
godel show <run_id> --graph         # inspect the DAG
godel rewind <run_id> --to <eid>    # undo a bad decision
godel repair <run_id>               # send an agent to unstick it
godel resume <run_id>               # continue
```

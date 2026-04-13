# Live Observability for Godel Workflows

## Context

A workflow run is currently a black box: one startup line, agent subprocess output captured and discarded, no live view except `godel tail` showing terse status rows. Users want to follow agent reasoning, tool calls, step tree, and timing — without compromising Godel's deterministic-replay guarantee.

## Architectural decision: file-as-interface, no in-process bus

Every frontend (TUI now, web UI / `godel watch <run_id>` later) is a **separate process** tailing on-disk files. The run writes; frontends read. No `EventBus`, no `ObservabilityBus`, no in-process pub/sub.

Why:
- **Isolation by construction** — a frontend cannot affect the run. The whole "observers must be read-only / swallow exceptions / sidecar-log failures" contract is replaced by process boundaries.
- **Replay-with-watch is trivial** — same code path as live; the renderer tails an existing transcript file.
- **Cross-process `godel watch <run_id>` is free** — same code as `--watch`.
- **No thread/contextvar gymnastics** — one writer, one ordering; agent reader threads just append lines stamped with their `step_path`/`stream_id` at construction time.
- **Backpressure becomes file rotation**, not queue discipline. Slow reader never blocks the run.

Cost: a bounded read latency (tunable flush cadence) and a harder commitment to the transcript file format as a versioned public interface. We'd pay that cost the moment `godel watch <run_id>` shipped anyway.

No broker / Redis / daemon — single-machine, single-producer, one-or-two-consumer fan-out. Local files win.

## Current-state anchors

- Append-only JSONL log — `godel/_event_log.py:150-165`
- Single `agent.call` per call, truncated — `godel/agents/_common.py:113-118`
- Copilot already streams JSONL internally, discarded — `godel/agents/_copilot.py:16-20`
- Subprocess stdout captured + dropped — `godel/_run.py:68-150`
- Existing async tail — `godel/_tail.py:84-183`; formatter `godel/cli.py:456-479`
- `parallel` in `godel/_decorators.py` — multiple agents active at once

## Design

### Two files, two roles

| | Canonical log (existing) | Transcript (new) |
|---|---|---|
| Path | `runs/<id>/events.jsonl` | `runs/<id>/transcript.jsonl` |
| Content | `step.enter`, `step.exit`, `agent.call`, `run` — stable schema, drives replay | `agent.thought`, `agent.tool_call`, `agent.tool_result`, `agent.raw`, `stdout` — advisory, rich |
| Replay | Drives replay | Never drives replay; served as-is |
| Size | Small, bounded | Rotated, size-capped, redacted at write |

Canonical log stays exactly as today — no shape change. All new richness goes in the transcript.

### Transcript file format — a versioned public interface

- JSONL, one event per line.
- First line is a header: `{"v": 1, "run_id": "...", "started_at": "..."}`. Readers refuse unknown major versions loudly.
- Every event has: `ts`, `seq`, `op`, `step_path`, `stream_id`, plus op-specific fields.
- `stream_id` is stamped at agent-subprocess launch time (captured in the closure of the reader thread) — not via contextvars.
- Documented in `docs/transcript-format.md`. Schema changes follow semver on the `v` field.

### Rotation policy (the interface contract under load)

- Cap at configurable size (default 50 MB). On rotation, current file is renamed `transcript.jsonl.1`, new `transcript.jsonl` is opened; older files shift to `.2`, `.3`, …
- **Never delete within a run.** Users can scroll back across rotated files.
- TUI scrollback follows the rotation chain explicitly.
- After-run retention: a separate `godel prune` story — not in this plan.

### Richer agent events

Parse Claude/Copilot streaming JSONL into transcript events. Hardening from day one:

- **Schema-tolerant parser** — unknown shapes → `agent.raw` event, never crash the run.
- **Line reader** — handles partial reads, >64 KB lines, CRLF, non-UTF8 (`errors="replace"`).
- **Burst coalescing** on read side (TUI), not on write side — writer just appends.
- **Parse-xor-tee** — if `--verbose`, raw bytes go to a separate `raw.log`, never the same fd being parsed.
- **Per-workflow opt-in** — `@workflow(stream_agents=True)` until proven stable.
- **Discoverability**: when `--watch` is set but `stream_agents=False`, TUI shows a one-line hint pointing at docs. Addresses the "two layers of opt-in = silent emptiness" trap.

### Redaction — infrastructure, not patterns

- Ship the redaction **hook**; ship **zero** built-in patterns in v1.
- `@workflow(redact=[...])` accepts user-supplied callables `(event) -> event`.
- **Ordering is explicit**: redactors run in registration order; doc says so; tested.
- Runs on the writer thread at transcript-write time. If a redactor raises, the event is dropped and a `redactor.error` event (with redactor name, no payload) is written in its place.
- Doc is loud: "Godel does not guess at what's a secret. Register redactors for your threat model."

### Stdout capture — explicit, opt-outable, documented

This is the single riskiest piece and gets its own subsection.

- **Off by default.** Enabled per-step via `@step(capture_stdout=True)` or per-workflow via `@workflow(capture_stdout=True)`. `--watch` does *not* silently enable it.
- Implementation: fd-level `os.dup2` so subprocess children inherit; restore on step exit via a `try/finally`. Not `sys.stdout` monkeypatching.
- Captured bytes are written as `stdout` events to the transcript (`{op: "stdout", step_path, chunk}`), line-buffered.
- `pdb`/`breakpoint()` incompatibility is documented; we detect an attached TTY on fd 0 and skip capture when a debugger is likely active (heuristic: `sys.gettrace() is not None`).
- Docs call out explicitly: "enabling stdout capture changes the semantics of print/logging in your step code."

### `godel tail` and formatter evolution

- Formatter uses a **registry**: ops self-register a one-line formatter. Default fallback renders `op step_path status (duration)` for unknown ops — no `?op` noise, no allow-list rot.
- Old JSONL logs still parse (regression fixture).

### TUI (`godel run --watch`)

- Optional dep: `rich` under `godel[watch]`. Never auto-enabled.
- **Model/render split**: renderer observes a plain `WatchModel`. Tests assert on model state, not rendered frames.
- **Parallel-aware**: step-tree pane on left; right pane has one collapsible panel per active `stream_id`, auto-tabbed beyond 3.
- **Coalesce on read**: TUI batches >N events / 100 ms into a single model update. Writer is never slowed.
- **Terminal hazards**: disable on non-TTY; plain prefixed line-log on `TERM=dumb`/non-UTF8; handle SIGWINCH, SIGTSTP, SSH drop via Rich `Live` + signal cleanup.
- **Replay-with-watch**: literally the same code reading the already-written transcript. No hollow-run bug.
- **Scrollback across rotation**: follows `.1`, `.2`, … chain.

## Files to touch (first slice)

- `godel/_transcript.py` (new) — writer, rotation, redactor registry, header/versioning
- `godel/agents/_copilot.py`, `_claude.py`, `_common.py` — streaming JSONL parser; transcript writes
- `godel/_run.py` — line-buffered stream reader; `dup2`-based stdout capture helper (opt-in)
- `godel/_decorators.py` — `stream_agents`, `capture_stdout`, `redact` workflow/step options
- `godel/_watch.py` (new) — `WatchModel` + Rich renderer + tail-chain reader
- `godel/cli.py` — `run --watch`; formatter registry refactor; `godel watch <run_id>` trivial wrapper
- `godel/_tail.py` — rotation-chain aware variant (shared with `_watch.py`)
- `pyproject.toml` — `rich` under `[project.optional-dependencies] watch`
- Docs: `transcript-format.md`, redaction guide, stdout-capture caveat

## Verification

- Unit: transcript writer appends in order; rotation preserves ordering across `.N` files; header version mismatch is refused loudly
- Unit: parser tolerates malformed JSONL, >64 KB lines, non-UTF8 bytes; unknown shapes → `agent.raw`
- Unit: redactors run in registration order; raising redactor yields a `redactor.error` event and does not drop subsequent events
- Unit: `stream_id` stamping survives thread-pool workers under `parallel`
- Unit: `WatchModel` state transitions on fixture event streams (no Rich rendering in tests)
- Integration: `examples/` workflow with `--watch` + `stream_agents=True`; reasoning and tool calls appear live; `Ctrl+C` restores terminal
- Integration: **late-attach** — start run, attach `godel watch <run_id>` midway, confirm TUI catches up from file
- Integration: replay-with-watch renders the same panels as live
- Integration: stdout capture works across a step that spawns a subprocess printing to stdout; disabling via `capture_stdout=False` restores stock behavior
- Regression: old-format `events.jsonl` fixture loads; `godel tail` formats it
- Perf (manual benchmark, not CI): 4-parallel-agent workload at sustained peak rate; writer latency budget documented from measurement, not asserted a priori

## Decisions (confirmed)

- Scope this iteration: transcript file + richer agent events + Rich TUI + `godel watch <run_id>` (falls out for free).
- No in-process event bus.
- Deps: optional extras (`godel[watch]`). Core stays lean.
- Defaults: `--watch` opt-in; `stream_agents` opt-in per workflow with a discoverability hint; `capture_stdout` opt-in with a loud doc warning; redaction ships infrastructure only, no built-in patterns.

## Out of scope

- Web UI (straightforward follow-up — same transcript file, new renderer)
- Token/latency metrics dashboard (cheap once `agent.tool_call`/`agent.tool_result` exist; separate proposal)
- Post-run retention/pruning policy beyond "no deletion within a run"
- Windows-specific subprocess quirks beyond "doesn't crash"

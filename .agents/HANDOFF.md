# Handoff: M0 Python Library Skeleton

## Goal
Implement the M0 milestone for the Godel Python library — the minimal package structure and primitives to run one real end-to-end workflow with a live Claude Code agent.

## What Was Done

**7 tickets completed** across 4 waves (parallelized where dependencies allowed):

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package config, deps: pydantic, click, python-ulid |
| `godel/_context.py` | WorkflowContext dataclass + ContextVars |
| `godel/_decorators.py` | `@workflow`, `@step`, `WorkflowFail`, `parallel()`, `retry()` |
| `godel/_run.py` | `run()` async subprocess primitive with `_privileged` flag |
| `godel/io.py` | Async `print`/`input` shadows |
| `godel/agents/_claude.py` | `claude_code()` factory wrapping `claude` CLI via `run()` |
| `godel/cli.py` | `godel run <file>` workflow discovery and execution |
| `examples/pr_review.py` | Live PR review workflow (validated end-to-end) |

**30 tests** across 5 test files, all passing.

## Current State
- All committed and pushed to `origin/main`
- Tests pass, `pip install -e .` works
- `requires-python` is `>=3.10` (system has 3.10.12, tickets spec'd 3.11)
- Beads tickets all closed, dolt pushed

## Live Validation Results
The PR review workflow ran successfully against real infrastructure:
- Created `feat/version-helper` branch, committed code, pushed
- Opened draft PR #7 on `atscub/godel-lang`
- Copilot reviewed and left 4 comments on intentional code smells
- Engineer agent categorized feedback, fixed 3 issues, re-ran quality gates
- Was killed during second review poll loop (no new comments); PR closed manually

## Gotchas

1. **`claude -p` returns natural language, not JSON** — even when prompted for schema output, the agent's `result` field is a summary of what it did. The `_claude.py` extraction fallback (haiku call) handles this, but it adds latency and cost per schema call.

2. **Agents operate in the working directory** — the engineer agent during the live run picked up uncommitted `_claude.py` edits and "fixed" them as part of Copilot review feedback. Real workflows should run in an isolated worktree/clone.

3. **`wait_for_review` polls with agent calls** — each poll is a full Claude invocation (~$0.05). Deterministic operations like PR comment polling should use direct `gh api` calls instead.

4. **`handle_feedback` doesn't pass comments to the prompt** — fixed in the final version, but worth checking: the `comments` parameter must be interpolated into the engineer's prompt string.

## What's Left (M1+)

- **M1** (`awl-gwj`): Audit log + JSONL persistence — event emission for every step/run/fork/join
- Granular permissions for `claude_code()` (currently `skip_permissions: bool`, should support `allowedTools` lists)
- Workflow isolation (run in separate worktree)
- Review polling via deterministic API calls instead of agent calls

## Entry Points
- **Spec**: `docs/py-library/02-api.md` (API contract), `docs/py-library/03-runtime.md` (execution model)
- **Code**: Start with `godel/_decorators.py` (core primitives) and `godel/agents/_claude.py` (agent interface)
- **Run**: `cd py-library && pip install -e . && python -m pytest tests/ -v`
- **Beads**: `bd ready` for next available work, `bd show awl-gwj` for M1 epic

---

# Handoff: M1 + M2 + M3 Implementation (2026-04-12)

## Goal
Implement M1 (audit log + JSONL persistence), M2 (godel.strict mode), and M3 (deterministic replay + resume) across 27 beads tasks organized into 9 dependency-ordered waves.

## What Was Done

**22 of 27 tasks completed** across Waves 1-6 (plus partial Wave 7). M1 and M2 are fully complete. M3 is partially complete.

### Completed Epics
- **M1 (`awl-gwj`)**: Audit log + JSONL persistence — DONE
- **M2 (`awl-7no`)**: godel.strict mode — DONE

### New Files Created

| File | Purpose |
|------|---------|
| `godel/_events.py` | `Event` dataclass + `EventStatus` enum (STARTED/FINISHED/FAILED/INVALIDATED/SUSPENDED) |
| `godel/_event_log.py` | `EventLog` class — in-memory DAG + append-only JSONL writer at `./runs/<run_id>.jsonl` |
| `godel/_exceptions.py` | `GodelStrictError`, `StrictViolation`, `ResumeError`, `UnsafeResumeError` |
| `godel/_strict_ast.py` | Layer 1: AST pre-scan for banned calls/modules |
| `godel/_strict_imports.py` | Layer 2: `sys.meta_path` import guard |
| `godel/_strict_audit.py` | Layer 3: PEP 578 audit hook (permanent, uses `_privileged` contextvar bypass) |
| `godel/_replay.py` | `ReplayWalker` (cursor-based DAG traversal), `ReplayMatch`, `MismatchPolicy`, hash mismatch handling |
| `godel/_dag_render.py` | ASCII DAG renderer for `godel show --graph` |

### Modified Files

| File | Changes |
|------|---------|
| `godel/_context.py` | Added `event_log`, `replay_walker`, `_invocation_counts`, `_step_local_seq` fields to `WorkflowContext`; added `get_event_log()` helper; added `_pending_replay` contextvar |
| `godel/_decorators.py` | `@workflow` creates EventLog + emits WORKFLOW_STARTED/FINISHED/FAILED, stores `_last_run_id`; `@step` emits step.enter events with invocation tracking; `parallel()` emits FORK/JOIN events |
| `godel/_run.py` | `run()` emits two-phase events (STARTED/FINISHED/FAILED), truncates stdout/stderr to 1000 chars in log; **replay guard added** — returns cached result on replay |
| `godel/io.py` | `print()`/`input()` emit events; **replay guards added** — print skips on replay, input returns cached value |
| `godel/det.py` | Replaced stubs with real implementations recording events; **replay guards partially added** (now/random have guards, uuid4 may be partial) |
| `godel/agents/_claude.py` | `__call__` emits agent.call events; extracted `_execute` method |
| `godel/cli.py` | Added `--strict` flag, `show` command with `--graph`, run_id output after execution |
| `godel/__init__.py` | Exports: Event, EventStatus, EventLog, get_event_log, GodelStrictError, StrictViolation, ResumeError, UnsafeResumeError, det |

### Test Files (27 total, 154 tests passing)

New test files: `test_events.py`, `test_event_log.py`, `test_exceptions.py`, `test_strict_ast.py`, `test_strict_imports.py`, `test_strict_audit.py`, `test_cli_strict.py`, `test_cli_show.py`, `test_workflow_events.py`, `test_step_events.py`, `test_run_events.py`, `test_io_events.py`, `test_det.py`, `test_parallel_events.py`, `test_agent_events.py`, `test_exports.py`, `test_exports_strict.py`, `test_strict_integration.py`, `test_integration_audit.py`, `test_replay_walker.py`, `test_dag_render.py`, `test_hash_mismatch.py`

## Current State (2026-04-12)
- **193 tests passing** (`python -m pytest tests/ -v`) across 29 test files
- All M1, M2, M3 milestones **complete**
- All beads tasks closed, dolt pushed

## M3 Completion Summary (Waves 7-9)

### Wave 7 — Completed
- `awl-571`: Hash mismatch detection — `_replay.py` with `handle_hash_mismatch`, `_cascade_invalidate`, `MismatchPolicy`
- `awl-8cy`: Resume exceptions — `UnsafeResumeError` with cmd/step_path/event_id attributes, actionable fix suggestions
- `awl-9z9`: Replay guards in all primitives — `run()`, `print()`, `input()`, `det.now()`, `det.random()`, `det.uuid4()`

### Wave 8 — Completed
- `awl-qj0`: Replay-aware `parallel()` — FORK invocation tracking, branch primitives replay from cache individually
- `awl-xj2`: CLI `godel resume <run_id> <file>` — loads JSONL, sets up ReplayWalker via `_pending_replay` contextvar, `@workflow` reuses run_id on resume

### Wave 9 — Completed
- `awl-e7i`: E2E integration test — 7 tests: crash-and-resume, det value stability, print silence, UnsafeResumeError, parallel branch replay, no duplicate subprocess, event append verification

### New Test Files (M3)
| File | Tests |
|------|-------|
| `test_replay_primitives.py` | 8 — replay guards for all primitives |
| `test_resume_exceptions.py` | 15 — exception hierarchy, attributes, formatting |
| `test_replay_parallel.py` | 4 — FORK/JOIN replay, invocation tracking |
| `test_cli_resume.py` | 5 — CLI resume command, workflow decorator resume path |
| `test_integration_resume.py` | 7 — E2E crash-and-resume validation |

## Key Design Decisions

1. **Audit hook test isolation**: All `--strict` CLI tests and audit hook tests use `subprocess.run()` because PEP 578 hooks are permanent. Never use CliRunner for tests that install audit hooks.

2. **EventLog file writes use `_privileged`**: The EventLog wraps all file I/O in `_privileged.set(True)` to bypass the audit hook in strict mode.

3. **`urllib` → `urllib.request`**: Changed banned module from `urllib` to `urllib.request` because `urllib.parse` is used by `pathlib` (stdlib dependency).

4. **Replay index key**: `(step_path, invocation_seq, step_local_seq, op)` — NOT event_id. This makes deterministic replay work because strict mode guarantees the same logical position on re-execution.

5. **JSONL is append-only**: STARTED appears first, then FINISHED/FAILED overwrites on load (last snapshot per event_id wins).

6. **Branch replay is implicit**: `parallel()` doesn't skip execution during replay — it re-enters all branches, and each branch's leaf primitives individually consult the ReplayWalker. FORK invocations are tracked with a `__FORK__` suffix key.

7. **Resume appends to same JSONL**: On resume, `@workflow` reuses the original `run_id` and `EventLog` (open for append). New events get new `event_id`s and higher `seq` numbers.

## What's Left (M4+)

- **M4** (`awl-dyn`): Rewind — rollback to a previous checkpoint
- **M5** (`awl-9lf`): Structured exception hierarchy
- **M6** (`awl-c8t`): Workflow linter
- **M7** (`awl-qe6`): Intervention mode
- **M8** (`awl-9g1`): DSL ↔ library interop (stretch)

## Entry Points

- **Spec**: `docs/py-library/02-api.md` (API contract), `docs/py-library/03-runtime.md` (execution model)
- **Code**: Start with `godel/_decorators.py` (core primitives) and `godel/_replay.py` (replay engine)
- **Run**: `cd py-library && pip install -e . && python -m pytest tests/ -v`
- **Beads**: `bd ready` for next available work

---

# Handoff: M4 + M5 + M6 + M7 (2026-04-13)

## Goal
Land M4 (Rewind), M5 (Structured exception hierarchy), M6 (Workflow linter), and M7 (Intervention mode) — including the `godel pause`, `godel resume`, `godel rewind`, and `godel repair` CLI surface plus the default intervention agent.

## Current State
- **561 tests passing** (`cd py-library && uv run pytest`)
- Working tree clean; master @ `2a1aa3c`; all commits pushed
- M4, M5, M6, M7 milestones **complete** end-to-end. M7 exit criterion (a) — `godel repair` auto-fixes a schema-mismatch typo without human input — verified by E2E test `tests/test_repair_e2e.py`.

## New / Modified Files (since M3)

| File | Purpose |
|------|---------|
| `godel/_rewind.py` | `rewind()` primitive + `apply_rewind()` (graph cut, INVALIDATED cascade, REWIND intent/outcome events, safety guard) |
| `godel/_pause.py` | Pause-sentinel file (`./runs/<run_id>.pause`) — atomic write via `mkstemp` + `os.replace`, per-run-id orphan glob cleanup |
| `godel/_linter.py` | Workflow linter — `@runtime_checkable` `LintRule` Protocol, `register_rule`/`clear_rules`/`get_rules`, `LintDiagnostic` with col 0-based and severity Literal-validated |
| `godel/_exceptions.py` | Two disjoint hierarchies: `GodelStrictError` (engine guard) and `GodelError` (workflow-author errors: `AgentRefusal`, `SchemaValidationFailure`, `HumanTimeout`, `NonDeterministicEscape`, `RewindUnsafe`); subclasses use `**kwargs` forwarding to the base |
| `godel/intervention/_context.py` | `InterventionContext`, `FailureInfo`, `SourceFile` + `build_intervention_context(run_id)` — reconstructs failure, local-state snapshot, sources from the audit log |
| `godel/intervention/_tools.py` | `InterventionToolset` — `edit_file`, `resume`, `give_up`, `rewind` returning `RewindResult` (incl. `already_rewound_ids`) |
| `godel/intervention/default_agent.py` | Default LLM repair agent — closure-factory `_make_intervention_workflow` keeps `@workflow` args slim; per-iteration `@step(name=f"reason_and_call_{i}")`; `_escape_backticks` prevents prompt injection |
| `godel/cli.py` | New subcommands: `godel pause <run_id>`, `godel rewind <run_id> --to <ids>`, `godel repair <run_id> [--agent MOD:FN] [--dry-run]` (exit codes: 0=resume, 1=give_up, 2=usage, 3=crash) |
| `tests/test_repair_e2e.py` | M7 exit-criterion E2E: deterministic mock intervention auto-fixes a Pydantic schema typo via real `godel repair` CLI subprocess |

(Plus extensive new tests across `test_rewind_*.py`, `test_pause_*.py`, `test_linter_framework.py`, `test_structured_exceptions.py`, `test_intervention_*.py`, `test_repair_cli.py`, `test_step_event_history.py`.)

## Key Design Decisions (M4–M7)

1. **REWIND emits two events: `phase=intent` (from the primitive) + `phase=outcome` (from `apply_rewind`)** — the pair makes the audit log unambiguous when a rewind operation produces different "requested" vs. "actually invalidated" sets.

2. **`rewind(to=[])` raises `ValueError`** before any side effects — empty target lists were silently producing empty REWIND events with no graph cut.

3. **`already_rewound_ids` on `RewindResult`** — targets that were already INVALIDATED are returned separately so the intervention agent can distinguish "no-op rewind" from "successful invalidation."

4. **Pause sentinel uses atomic write** — `mkstemp(dir=parent, suffix=f".{run_id}.pause.tmp")` + `os.replace`; `clear_pause_request` globs scoped to `*.{run_id}.pause.tmp` so concurrent runs are not affected.

5. **Per-branch replay-suppress flag (`WorkflowContext._local_replay_suppress`)** — a sibling parallel branch reaching a non-cached step boundary used to clear the *shared* `event_log._replay_suppress` and corrupt the cached branch's `last_step_event_id()`. Each branch now snapshots the flag at fork time; `_clear_local_suppress()` relies on `asyncio.gather`'s `copy_context()` task isolation to mutate only the calling branch's context.

6. **Source-edit guard normalizes whitespace before hashing** — `inspect.getsource()` is `rstrip()`-ed and consecutive blank lines collapsed before SHA-256, so trivial reformat doesn't trip the resume edit detector. Documented limitation: triple-quoted string content is not normalized.

7. **Default intervention agent uses a closure-factory** — `@workflow`'s default `repr(args)` capture would have dumped the entire `InterventionContext` (events + sources) into the audit log. The factory pattern keeps the `@workflow`-visible signature to `(run_id: str, run_state: str)`; `ctx`/`tools` are captured via closure.

8. **`SchemaValidationFailure` exists in two namespaces** — `godel._exceptions.SchemaValidationFailure` (subclass of `GodelError`, raised by the engine on Pydantic validation) and `godel.agents.SchemaValidationFailure` (subclass of `WorkflowFail`, used by agent factories). They are intentionally distinct; do not unify without auditing all `isinstance` callers.

## Gotchas / Open Follow-ups

1. ~~**`_replay_suppress_clear_gen` counter is dead code** (filed: `awl-ddk`) — incremented in 3 places but never read. Either wire up a debug consumer or remove.~~ ✓ fixed in f70639d

2. ~~**`test_parallel_mixed_cached_race_last_step_event_id` asserts ordering** (`awl-ddk`) — `_step_event_history` is documented as non-deterministic across parallel branches; the test happens to pass today but should switch to set/sorted membership.~~ ✓ fixed in f70639d

3. **`_render_context_marker` doesn't strip whitespace-only step_path components** (`awl-uni`) — `'   '` is truthy and slips through; fix should use `s.strip()` not bare `if s`.

4. **`**kwargs` forwarding in `GodelError` subclasses kills IDE param hints** (`awl-uni`) — typos surface as `TypeError` from `GodelError.__init__` instead of at the subclass call site. Consider `typing_extensions.Unpack[TypedDict]` to restore static visibility.

5. **`repair` CLI `--agent MOD:FN` requires `_is_workflow=True`** on the resolved function. Bare `async def` is rejected. The default agent and the test's mock both use the closure-factory pattern: outer function carries the `_is_workflow` marker, inner `@workflow` does the audit work.

## What's Left (M8+)

- **M8** (`awl-9g1`): DSL ↔ library interop (stretch)
- Deferred WARN follow-ups: `awl-uni` (GodelError hygiene), `awl-ddk` (replay-suppress dead code + test ordering)
- Backlog (P4): `awl-ul2` parser error recovery, `awl-509` prompt success/failure tracking, `awl-a7p` workflow stdlib

## Entry Points

- **Spec**: `docs/py-library/02-api.md`, `docs/py-library/03-runtime.md`
- **Code**: `godel/cli.py` (`repair_cmd`), `godel/intervention/default_agent.py`, `godel/_rewind.py`
- **Run**: `cd py-library && uv run pytest`
- **Beads**: `bd ready`

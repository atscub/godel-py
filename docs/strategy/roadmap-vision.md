# Roadmap & technical vision

*Snapshot: 2026-04. Re-audit quarterly. Verify "shipped" claims against the current codebase before acting.*

## Shipped (pre-1.0, as of 2026-04)

The library currently implements — and has test coverage for — the primitives the pre-1.0 design docs targeted in M0–M4 plus most of M5–M7:

- `@workflow`, `@step`, `@retry`, `parallel`, `WorkflowFail`, agent factories (`claude_code`, `copilot`).
- Append-only JSONL event log with ULID event IDs, FORK/JOIN as first-class events, two-phase STARTED→FINISHED recording.
- `godel.strict`: AST pre-scan, `sys.meta_path` import guard, `sys.addaudithook` runtime guard, `godel.det.{now, random, uuid4, choice, randint}`.
- Deterministic replay via DAG walker; `godel resume` with `request_hash` mismatch detection (continue/invalidate/abort).
- `rewind(to=event_id)` with append-only INVALIDATED semantics, JOIN cascade-suspend, safety table for non-idempotent `run()`.
- Structured exceptions: `AgentRefusal`, `SchemaValidationFailure`, `CommandFailure`, `HumanTimeout`, `NonDeterministicEscape`, `RewindUnsafe`, `SourceEditedError`.
- `godel lint` with Python-specific rules (missing `await`, missing `@step`, non-determinism leaks).
- `godel pause` / `godel repair` intervention mode; source-hash guardrail with `--on-source-edit={warn,abort,ignore}`; default intervention agent bootstrapped as a workflow.
- Claude Code skills (`godel-runner`, `godel-engineer`) for authoring and repairing workflows.

See `docs/concepts.md` and `docs/api-reference.md` for authoritative user-facing documentation.

## Not yet shipped — the forward-looking moat

These are the pieces that extend the moat beyond what's in `docs/`. All are conditional — build when first users actually need them, not before.

### OS-level sandbox (Layer 4 of strict mode)

The current `godel.strict` is a tripwire for honest mistakes (accidental `requests.get`, `datetime.now`, writing to disk), not a security boundary. Adversarial code can escape via C extensions calling `libc.open` directly, `ctypes`, or by flipping the `_privileged` contextvar. For regulated-vertical buyers who need real isolation, add an opt-in Layer 4: `firejail --net=none --read-only=/`, Docker, or gVisor wrapping the worker process. The in-process audit hook becomes a cheap second layer instead of the only defence.

### DSL ↔ library interop

The DSL ships as a codegen frontend that transpiles to library Python. What this actually needs:

- Shared event-log schema used by both engines (already partly true — the runtime is one engine).
- `.gdl` callable as a procedure from Python, and `@workflow` callable from `.gdl`.
- One mixed example whose audit log replays correctly across the boundary.

The design disciplines that keep transpilation trivial are already being followed: canonical primitive names with 1:1 correspondence between library and DSL keywords (`@workflow`/`DEF`, `parallel`/`PARALLEL`, `@retry`/`RETRY`, `godel.print`/`NOTIFY`, `godel.input`/`PROMPT`, `run`/`RUN`), `godel.strict` from day one rejecting arbitrary-Python drift, and treating DSL adoption as codegen rather than a separate runtime bridge.

### Embedded use (handle-returning bootstrap)

`asyncio.run(fn())` blocks until completion and returns nothing, so pause/input/live-observation are impossible from an embedding caller. The path is straightforward: expose the same bootstrap that `godel run` uses as an importable function that returns a run handle. Design this when a real embedding use case lands.

### Multi-process / multi-host workers, remote storage

Deliberately out of scope until asked for. Storage progression stays JSONL → SQLite → remote. The library wins by being the thing you can try on a weekend, not the thing you operate a cluster for.

## The six technical claims the moat rests on

Each of these answers "can a larger model absorb this?" with *no* — which is the selection criterion for everything in the runtime layer:

| Primitive | Why weight absorption can't reach it |
|---|---|
| Durable replay from event log | A smarter model cannot make crashes un-happen. Durability is storage + re-execution. |
| Rewind | Requires deterministic replay + soft-delete log semantics. Not a prompting behaviour. |
| Hot-patch of `@step` on a paused run | Requires a process model that can pause, swap code, and resume. Outside the inference loop. |
| Repair-mode CLI | A smarter recovery agent still needs the primitives to act through. |
| Workflow as version-controlled code artifact | A file on disk; absorption into weights is category-incoherent. |
| `godel.strict` sandbox | A smarter model doesn't make `open("x", "w")` safe. |
| Audit log as primary artifact | External to the model; survives arbitrary capability growth. |

Things that *can* be absorbed (CoT scaffolds, self-consistency voting, tree-of-thought, domain-specific prompt patterns) have deliberately been kept out of the runtime. The load-bearing value lives below the prompt layer.

## Commercial roadmap — the parallel track

Engineering milestones are necessary but not sufficient. Execution on the [business-model.md](business-model.md) vertical-first strategy runs in parallel:

**Phase A — Runtime validation (weeks 1–11, overlapping core milestones).**

- Instrument a canonical example (PR review or similar) to measure real token cost on mid-tier vs frontier models for the same task shape. Produce the first concrete amortisation number on a real workload.
- Pick one candidate regulated vertical by week 6. Don't commit yet.
- Conduct 10+ practitioner discovery calls. Validate: task shape repeats ≥ monthly; incumbents are bolting-on; compliance requirements specifically include replay, audit, or human-in-the-loop approval.
- Go/no-go on the vertical by week 11. No-go means run another vertical through the same loop.

**Phase B — Vertical wedge (months 3–9).**

- Build a single-workflow MVP for the chosen vertical with Godel as invisible engine. Reasoning model authors; mid-tier executes.
- Ship to 1–3 design partners on outcome-based pricing. Instrument everything — per-outcome cost, cycle time, rewind/repair frequency.
- Grow the workflow library as an explicit product component.
- Target: first paying customer by month 6; ARR > $100K by month 9.

**Phase C — Vertical scale (months 9–18).**

- Add 5–10 customers in the same vertical; convert outcome-pricing into repeatable contracts.
- Measure reuse rate across customers as leading indicator of moat depth.
- Hire: first engineer on runtime/infra, second as vertical domain specialist.
- Target: ARR > $1M by month 18; gross margin > 75% driven by the amortisation arbitrage.

**Phase D — Harness spinoff decision (month 18+).**

Only if Phase C hits targets and an enterprise-sales co-founder candidate is identified. Otherwise, double down on the vertical — the vertical alone can be a strong business; the harness is a stretch goal, not a requirement.

*Sources: py-library/05-strategy.md §12, py-library/06-roadmap.md §14–§15, py-library/03-runtime.md §7.*

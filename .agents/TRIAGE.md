# GitHub Issue Triage Guide

When a new GitHub issue comes in, walk through these phases in order. Every issue must exit triage with labels applied and a comment posted.

## Labels

| Label | Purpose |
| ----------- | ------------------------------------ |
| `bug` | Broken behavior |
| `feature` | New capability request |
| `question` | How-do-I, is-this-possible, unclear |
| `P0` | Critical — security, data loss, total crash |
| `P1` | High — broken public API, determinism violation |
| `P2` | Medium — broken with workaround, doc gaps |
| `P3` | Low — ergonomic, better errors, edge cases |
| `P4` | Backlog — nice-to-have, cosmetic, speculative |
| `needs-info` | Missing required information |
| `wontfix` | Rejected — out of scope or against principles |
| `duplicate` | Already tracked in another issue |

## Phase 1: Classify type

Read the issue and assign exactly one type label:

- **`bug`** — reports broken behavior. Look for: error tracebacks, "expected X got Y", steps that reproduce a failure.
- **`feature`** — requests new capability. Look for: "it would be nice if", "can godel support", "add X".
- **`question`** — everything else. How-do-I, is-this-possible, usage help, unclear reports that might be bugs but lack enough detail to tell.

When ambiguous between `bug` and `question`, default to `question` and ask for clarification.

## Phase 2: Assess priority

Assign exactly one priority label using these rules. Work top-down — the first matching rule wins.

### P0 — Critical

- Security vulnerability (command injection, path traversal, unsafe deserialization, secret exposure)
- Event log corruption or data loss (events silently dropped, log truncated, replay produces wrong state)
- Complete workflow crash with no workaround (any workflow hits this, not just an edge case)

### P1 — High

- Broken public API: `@workflow`, `@step`, `parallel`, `retry`, `run()`, or any CLI command produces wrong results or crashes
- Determinism violation: a replayed workflow produces different results than the original run
- Regression: something that worked in a previous release is now broken

### P2 — Medium

- Incorrect behavior that has a known workaround
- Missing or misleading error messages that cause users to waste significant time
- Documentation gaps for existing, shipped features
- Bugs in non-core paths (examples, dev tooling)

### P3 — Low

- Ergonomic improvements to existing features (better defaults, less boilerplate)
- Better error messages where the current ones are unhelpful but not misleading
- Non-critical edge cases that affect unusual configurations

### P4 — Backlog

- Nice-to-have features with no pressing use case
- Cosmetic issues (output formatting, log aesthetics)
- Speculative ideas without a concrete user need behind them

**Features** get priority based on the same scale — a feature that addresses a P0-level gap (e.g. "there's no way to handle secrets safely") ranks higher than a cosmetic feature request.

**Questions** don't get a priority label unless they reveal an underlying bug or feature request during triage.

## Phase 3: Check required info

Before triaging further, verify the issue contains enough information. Requirements vary by type:

### Bugs must include:
- godel version (`godel --version` or `pip show godel`)
- Python version
- Reproduction steps OR a full error traceback
- Expected vs actual behavior

### Features must include:
- A concrete use case (not just "add X" — why do they need X?)

### Questions:
- No strict requirements, but if the question is too vague to answer, treat as needs-info.

**If required info is missing:** apply `needs-info`, post a comment requesting the missing pieces (see response templates below), and stop. Do not assign priority until info arrives.

## Phase 4: Reject or accept

Check whether the issue should be rejected. Close immediately with the appropriate label if any rule matches.

### Reject as `wontfix` when:

- **Out of scope** — the issue is about the user's workflow logic, their agent's behavior, a third-party library, or infrastructure (CI, deployment, hosting). Godel is the orchestrator; problems in what it orchestrates are not godel issues.
- **Against core principles** — the request would violate determinism, break replay guarantees, bypass the event log, or introduce non-deterministic operations into the step/workflow layer. Examples: "let me use `random.random()` in a step", "skip event logging for fast steps", "add mutable global state across steps".
- **Already solved** — godel already supports this, the user just doesn't know how. Convert to `question`, answer it, and close.

### Reject as `duplicate` when:

- An open issue already covers the same problem or request. Link to the existing issue in your closing comment.

### Not actionable:

- If the issue is vague, has no concrete use case, and the reporter was asked for clarification (via `needs-info`) but didn't respond within a reasonable time, close as `wontfix` with a note that it can be reopened with more detail.

**If none of these apply**, the issue is accepted. Proceed to respond.

## Phase 5: Respond

Post a comment on the issue. Use the appropriate template below, adjusted to fit the specific issue. Keep the tone direct and helpful.

### Triaged (accepted)

```
Thanks for reporting this. Triaged as {type} / {priority}.

{One sentence on what happens next or what the fix likely involves, if obvious.}
```

### Needs info

```
Thanks for opening this. Before we can triage, we need a bit more detail:

- {missing item 1}
- {missing item 2}

Once you add that, we'll prioritize it.
```

### Duplicate

```
This is covered by #{existing_issue_number}. Closing as duplicate — feel free to add
your context to that issue.
```

### Wontfix — out of scope

```
This looks like it's about {what it's actually about} rather than godel itself.
{Pointer to the right place if possible — e.g. "You might want to file this
against {library}" or "This is a question about your workflow logic rather
than the orchestrator."} Closing for now, but feel free to reopen if I've
misread this.
```

### Wontfix — against principles

```
Godel's replay and determinism guarantees depend on {the specific principle
this violates}. Supporting this would break {concrete consequence}.

{If there's an alternative approach that works within godel's model, suggest it.}

Closing as wontfix, but happy to discuss if you see an angle we're missing.
```

### Wontfix — not actionable

```
Closing this for now since we don't have enough detail to act on it. If you
can add a concrete use case or reproduction steps, feel free to reopen.
```

# Positioning

*Snapshot: 2026-04. Re-audit quarterly.*

## The moat

Six properties compound into something harder to replicate than any one of them suggests. The first three are technical, the last three strategic.

1. **Rewind + hot-patch + resume as an integrated triad.** Every competitor has at most one or two. Temporal has resume but not rewind. LangGraph has checkpoints but not arbitrary-position rewind. Claude Code has neither. The triad is only possible because the runtime enforces determinism and stores an event-sourced log — the same two properties that make replay possible make rewind possible for free. Patching is file editing: pause, edit the `.py`, resume; `rewind` first if a completed step needs to re-execute.

2. **Intervention as a product surface, not an escape hatch.** Most durable-execution systems treat "what happens when a workflow fails" as an ops concern — a UI, a retry button, a runbook. None have a story for "the run is still running but it's obviously going wrong." Godel answers both: another agent (or human) can fix a crashed run, *and* a caller watching the live audit log can fire `godel pause` the moment the stream shows drift, then patch or rewind before budget is wasted. Reactive repair and proactive steering reach the same primitives through the same CLI.

3. **Two-mechanism efficiency.** Code-enforced flow wins on two independent axes against in-weight reasoning. *Amortisation*: on reused task shapes, a reasoning model authors the workflow once and a mid-tier model executes it forever. SPRINT (Stanford, 2025) reports a 39% token reduction on long-reasoning problems; CoThink (2025) reports 22.3% total token reduction at 0.42% accuracy delta — neither counts reuse, which is the multiplier. *Decomposition*: on tasks above the monolithic-reasoning ceiling, step-by-step execution in fresh per-step contexts is the only path that works. Amortisation is a cost play; decomposition is a capability play; regulated verticals typically win on both at once.

4. **Harness-level positioning.** "Let your top-level LLM write the workflow shape it needs, instead of baking shapes into the product" is unique. Current harnesses (Cursor, Cognition/Windsurf, Factory, Augment, Replit) bake workflow shapes in. LangGraph comes closest but requires graph pre-declaration. The library lets shape emerge from whatever the LLM writes that run, and the runtime guarantees reliability regardless.

5. **Agent-first primitives + audit log as primary artifact.** Agents are values — callables the author composes, parameterises, and stores. Session state lives in closures; schemas are Pydantic-validated. The audit log is read by humans and recovery agents, diffed across runs, checked into git alongside workflow code. Competitors would have to break backwards compatibility to add this — Temporal's Workflow/Activity model doesn't have a place to attach session state to a call site.

6. **Vendor neutrality.** Claude, Codex, Copilot, Gemini, and open-source local models all plug in as agent factories. Regulated buyers in banking, healthcare, and government *require* vendor neutrality for data-residency and single-vendor-risk reasons. This is the Temporal playbook.

## Competitive landscape (April 2026)

```
                IMPERATIVE         GRAPH/DAG          STATE MACHINE
              ┌─────────────────┬─────────────────┬─────────────────┐
DURABLE       │  ★ godel ★      │                 │  Step Functions │
+ AGENT-FIRST │                 │                 │                 │
+ REWIND      │                 │                 │                 │
              ├─────────────────┼─────────────────┼─────────────────┤
DURABLE       │ Temporal, DBOS  │ Airflow,Prefect │  Step Functions │
(generic)     │ Restate         │ Dagster         │                 │
              ├─────────────────┼─────────────────┼─────────────────┤
NOT DURABLE   │ CrewAI, AutoGen │  LangGraph      │                 │
(agent-first) │ Swarm           │                 │                 │
              └─────────────────┴─────────────────┴─────────────────┘
```

Rough numbers to keep in mind (verify before quoting externally):

- **LangGraph** — 28k stars, LangChain $260M backing, v1.1.4 GA. Optional determinism, checkpoint-based resume, no rewind.
- **Google ADK** — 18.7k stars, Google-backed. Optional determinism, tree-only (single-parent) composition, LoopAgent only.
- **Flyte** — 6.9k stars, Union.ai $38M+, 8 years. Deterministic DAG, no cycles, no human-in-the-loop native.
- **Temporal** — $650M raised, $5B valuation, 380% growth. Durable execution, agent-adjacent not agent-first. 10–50ms overhead per activity is wrong for LLM-latency tasks.
- **CrewAI** — $18M Series A, ~$3.2M revenue, role-based. No durability.
- **Cursor / Cognition (Devin + Windsurf) / Factory / Augment / Replit** — harness builders, consolidating in-house, not licensing third-party runtimes.

## Where *not* to compete

Saturated or structurally wrong:

- **AI code review** — CodeRabbit, Greptile, Graphite. Cognition consolidating via Windsurf.
- **AI SRE / incident response** — Resolve AI ($125M @ $1B, Feb 2026), Incident.io, Rootly.
- **Visual / no-code workflow marketplaces** — Gumloop ($50M, March 2026), Lindy, Relevance AI, n8n AI, Zapier Agents. They own drag-and-drop.
- **Legal AI, enterprise search, IDE agent harnesses** — Harvey, Glean, Cursor. Saturated or consolidating.

## Where to compete

**Underattacked regulated niches** where rewind + audit-log-as-compliance-artifact is a legal requirement, not a feature:

- Clinical trial protocol compliance and adverse-event triage
- SOX control evidence collection and audit prep
- FDA submission assembly (510(k), IND, NDA)
- Pentest reporting and security-finding triage
- Financial close reconciliation and variance investigation
- Insurance claims adjudication
- Regulated-industry runbook automation (utilities, banking, healthcare)

Common pattern: high ACV; **repeating task shapes** so the amortisation argument bites; **compliance requirements** that make rewind and audit-log-as-primary-artifact a *selling feature* rather than plumbing; **slow incumbents** bolting AI on instead of re-architecting.

## Regulatory tailwind — EU AI Act

The EU AI Act enforces **August 2, 2026**. Requirements for high-risk AI systems map onto Godel's primitives directly:

| EU AI Act requirement | Godel mechanism |
|---|---|
| Tamper-resistant logging | Append-only event log |
| Reproducible execution | Deterministic replay under `godel.strict` |
| Human-in-the-loop records | `godel.input` recorded with prompt + answer |
| Model version tracking | Agent factories carry explicit model IDs |
| Guardrail events | `@retry`, `WorkflowFail`, schema coercion raise structured exceptions |

Fines up to €35M or 7% of global revenue. AI governance market projected $5.8B by 2029. Funded competitors (Credo AI $41M, Notch $45M) are governance *dashboards* — they tell you what policies to follow. Nobody tells agents how to follow them deterministically. Amsterdam / Netherlands base gives local EU credibility; fintech hub with DNB / AFM; WBSO, MIT Regeling, SIDN/NLnet available as non-dilutive funding.

## The library-vs-DSL question

The library ships first because LLM write-success-rate is the primary bottleneck for agent-authored workflows, and Python is LLM-native. Axis-by-axis:

| Axis | Library | DSL |
|---|---|---|
| LLM one-shot write success | **Wins** (Python is LLM-native) | Needs full spec in context or fine-tuning |
| Traceback debuggability by LLM | **Wins** (LLMs know Python errors cold) | Custom error vocabulary |
| Pre-execution lint coverage | Catches shape errors after M6 lint work | Catches shape errors for free |
| Shape uniformity for patching | Many shapes; requires convention enforcement | One shape per construct |
| Human read-cost | ~80% of DSL with discipline | Global minimum (~20 keywords) |
| Native tooling | All of Python's ecosystem | Custom extensions required |

Net: library wins the agent-authored, runtime-correctable objective. The DSL ships later as a **codegen frontend** that transpiles to library Python, targeting read-heavy environments (compliance, cross-team audit, regulated flows where non-engineers must read workflows). Terraform HCL / Pulumi segmentation, not competition.

## Principal threats

1. **Anthropic or OpenAI ships first-party durable-execution** ("Claude Workflows" with built-in replay and schema coercion). Distribution advantage no third-party can match. Defence: vendor neutrality — regulated buyers require multi-vendor by law.
2. **Temporal adds agent primitives.** Breaking the Workflow/Activity split to add closure-based agents + schemas is not a free move; it breaks existing users. Overhead per activity is also wrong for LLM-latency tasks.
3. **LangGraph adds durable replay.** Replacing graph-first state machines with imperative code contradicts LangGraph's brand identity.
4. **Bitter-lesson absorption.** Prompt-level scaffolds (CoT, self-consistency, ReAct, tree-of-thought) get absorbed into reasoning-model weights. Godel's primitives live *below* the prompt layer — durable replay, rewind, hot-patch, the strict sandbox, the audit log as external artifact — which is category-incoherent with weight absorption. The selection of which primitives to ship has deliberately excluded anything a larger model can absorb.

*Sources: py-library/04-tradeoffs.md, py-library/05-strategy.md §12, viability-analysis-2026-04.md, market-analysis.md.*

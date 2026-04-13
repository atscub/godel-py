# Business model

*Snapshot: 2026-04. Re-audit quarterly.*

## The core commercial thesis

Industry benchmarks (SWE-bench, GAIA, agent leaderboards) measure single-shot task success on a frontier model. That's the right metric for research and the wrong one for economics. Real production workloads run the same task shapes — PR reviews, incident triage, compliance checks, runbooks, filings — hundreds of times per customer per month. The dominant cost is **amortised cost per outcome over reused task shapes**, not single-shot accuracy.

On that axis, two cost profiles diverge:

- **Reasoning-model path.** Per invocation: α·C tokens on chain-of-thought planning + β·C tokens on execution. The plan is re-derived every call, stochastic, opaque, un-reusable. Reasoning tokens are billed as output; a "simple" o3 response consumes 5–10× the visible output in hidden reasoning.
- **Code-enforced workflow path.** Per invocation: **zero** tokens on planning (the plan is code) + β'·C tokens on execution, where β' ≤ β because each subtask prompt is tighter and the model never decides what comes next.

Amortised across K reuses:

- Reasoning model: **K · (α·C + β·C)**
- Godel workflow: **1 · (authoring cost) + K · (β'·C)**

As K grows, the ratio asymptotes to β'/β < 1, and the authoring cost vanishes into the average. The second mechanism is **decomposition past the reasoning ceiling** — tasks above the single-trajectory ceiling, where the alternative to code-enforced decomposition isn't a cheaper solution, it's no solution. Amortisation is a cost play; decomposition is a capability play; regulated verticals typically win on both at once.

## The two usage patterns

**Pattern A — Amortisation.** K > 1. Reasoning model (o3, Opus 4.6, DeepSeek R2, Gemini 3.1 reasoning) authors the workflow once, commits it to version control. Mid-tier model (Sonnet 4.6, Haiku 4.5, 4o-mini, Llama-70B, Qwen) executes it forever. Pay planning cost once, amortise across invocations.

**Pattern B — Decomposition.** K = 1, but task exceeds the monolithic-reasoning ceiling. Reasoning model authors; a reasoning model (possibly the same one) executes each step in a fresh context. `godel pause`, `godel repair`, and `rewind` matter *more* here than in Pattern A — hard failures deep in a long run are prohibitively expensive to restart, and soft failures (silent drift onto the wrong subgoal) are only catchable by a caller watching the live audit log.

A workload with short self-contained one-off tasks gains nothing from Godel. The play is conditional on (a) repeating task shapes, (b) tasks above the reasoning ceiling, or (c) both.

## The leverage play vs the direct-sale play

There are two plausible go-to-market shapes. The solo-founder, zero-capital reality in April 2026 strongly favours the first.

**Godel as proprietary leverage.** Build products *with* Godel as the invisible engine. Revenue comes from what the orchestration produces, not from selling orchestration. One person outpacing teams becomes the moat. Precedent: Amazon didn't sell AWS first — they built a bookstore, infrastructure emerged. Stripe sold "accept payments in 7 lines", not "payment orchestration". Godel either stays proprietary forever, or gets open-sourced later from a position of strength.

**Godel as open-core product.** Open-source the library, sell cloud observability and enterprise features. Classic developer-tools playbook (JetBrains, Basecamp, Temporal). Requires community-building time, marketing budget, and distribution — all of which are scarce for a solo founder with zero capital, and all of which burn the 2026 timing window.

The leverage play is the recommended starting point. The open-core play becomes achievable later from revenue, not before.

## The vertical-first playbook

Horizontal-framework and harness-builder plays both need multiple co-founders and significant capital. Vertical-first is the only path achievable on a realistic early-stage timeline.

1. **Pick one underattacked regulated niche** with repeating task shapes. Ten practitioner conversations before committing. Verify the pain is real and paid, and that incumbents are bolting AI on rather than re-architecting. Shortlist candidates: clinical trial protocol compliance, SOX control evidence, pentest reporting, FDA 510(k) assembly. Pick by access — whichever domain delivers five practitioner calls fastest.
2. **Build the product with Godel as invisible engine.** Customer buys a compliance outcome; they never see the runtime. Durability, rewind, repair are load-bearing but not surfaced.
3. **Use the two-role pattern.** Reasoning model authors workflows; mid-tier executes. Publish nothing about the model stack — it's a cost-side trade secret.
4. **Price on outcomes.** $/control-verified, $/incident-triaged, $/filing-assembled, $/claim-adjudicated. Efficiency arbitrage becomes margin, not a customer discount.
5. **Workflow library as portfolio-level moat.** Every customer-authored workflow (with permission) joins a library that new customers inherit on day one. Reuse compounds across invocations within a customer *and* across customers within the vertical. Six months in, a new competitor has to pay reasoning-model authoring costs on every task shape from scratch.

## Sequencing (solo founder, zero capital)

- **Weeks 1–2.** Pick a product; ship MVP using Godel as engine.
- **Weeks 3–4.** Get 3–5 paying customers (even €50/month proves value).
- **Month 2.** Register eenmanszaak (free) or BV (~€500 via notary). Apply to Antler Amsterdam / Rockstart / YES!Delft (stipend + mentoring). Or approach angels with a working product + paying customers (€25–100k pre-seed).
- **Month 3+.** Revenue funds development. Apply for WBSO once payroll/subcontractor costs exist. MIT Regeling once a BV is registered.

Regulated / Amsterdam advantages: EU-based (EU AI Act enforces locally from Aug 2, 2026); fintech hub (Adyen, Mollie, Bunq; DNB and AFM headquartered); ecosystem (Holland FinTech, NLAIC, Rockstart, Startupbootcamp, ACE); non-dilutive grants (WBSO, MIT Regeling, SIDN/NLnet).

## The harness spinoff (phase two)

Once one vertical is profitable and the workflow library has depth, the harness spinoff becomes achievable — not before. The pitch is **not** "a better Cursor" (unwinnable). The pitch is **"the agent harness for regulated and audit-heavy environments"** — a segment Cursor/Cognition/Factory/Augment structurally don't serve, because compliance was never their priority.

Differentiators the current harness builders can't easily match without breaking their architecture:

- Every agent action deterministic, replayable, patchable
- Every run audit-logged by construction, not by instrumentation
- Workflows as versioned artifacts, not in-context scratchpads
- Rewind and repair as first-class recovery operations
- Model-neutral runtime (mandatory for data-residency)
- On-prem or customer-VPC deployment supported natively

This is a $5–20M Series A conversation with a co-founder running enterprise sales. **Don't start here.** Reach it from a profitable vertical that's validated the runtime against real compliance auditors.

## Re-audit triggers

Re-evaluate the entire strategy if any of these happen:

- A frontier model release absorbs one of Godel's runtime primitives (unlikely — runtime features, not prompt tricks).
- Anthropic or OpenAI ships a first-party durable-execution runtime ("Claude Workflows") with competitive replay.
- A well-funded competitor enters the same regulated vertical with similar architecture.
- Reasoning-model pricing drops > 10× (would compress the amortisation arbitrage).

## 90-day success

M0–M3 shipped; one vertical chosen with 10+ validation calls; a concrete measured token-cost-per-outcome number on a real workload; a go/no-go decision made with evidence.

*Sources: business-strategy.md, business-advice.md, brainstorm-leverage-strategy.md, py-library/05-strategy.md §13, viability-analysis-2026-04.md §5–§7.*

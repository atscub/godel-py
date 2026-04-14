"""Feature factory — end-to-end autonomous feature delivery.

Pipeline:
  1. PM-brainstorm agent proposes agent-testable feature ideas.
  2. PM-risk agent scores risk, picks one or loops back (max 3).
  3. Beads ticket created with non-technical acceptance criteria.
  4. Architect drafts plan, reviewer critiques (first-principles
     coherence + technical risk). Loop until approved (max 3).
  5. Fresh Engineer implements on a branch.
  6. In parallel: PM runs adversarial acceptance test, second
     Engineer runs code review. Loop implement on failure (max 3).
  7. Tidy up, print summary.

Human checkpoints via `input()` guard each major phase so a
supervisor can abort a derailed run.
"""
from pydantic import BaseModel
from godel import workflow, step, parallel, WorkflowFail, print, input
from godel.agents import claude_code


MAX_LOOPS = 3


class FeatureIdea(BaseModel):
    title: str
    problem: str
    solution: str
    acceptance_criteria: list[str]
    agent_testable: bool
    single_task_scope: bool


class IdeaBatch(BaseModel):
    ideas: list[FeatureIdea]


class RiskAssessment(BaseModel):
    idea_index: int
    risk_level: str  # low | medium | high
    rationale: str


class PMVerdict(BaseModel):
    chosen_index: int | None
    assessments: list[RiskAssessment]
    reason: str


class TicketRef(BaseModel):
    id: str


class PlanReview(BaseModel):
    approved: bool
    coherence_issues: list[str]
    technical_risks: list[str]
    required_changes: list[str]


class ImplementResult(BaseModel):
    branch: str
    commit_sha: str
    files_changed: list[str]
    summary: str


class AcceptanceReport(BaseModel):
    passed: bool
    failures: list[str]
    evidence: list[str]


class CodeReviewReport(BaseModel):
    approved: bool
    blocking_issues: list[str]
    nits: list[str]


@step
async def brainstorm(pm) -> IdeaBatch:
    return await pm(
        "You are a Product Manager for the godel-py project (deterministic "
        "orchestrator for AI agent workflows, Python). Propose 4 feature "
        "ideas. HARD CONSTRAINTS: each must be (a) scoped as ONE engineering "
        "task (~half day), (b) fully testable by agents with no human, "
        "(c) valuable to users. Read the repo (godel/, docs/, examples/) "
        "to ground yourself. Return ideas with non-technical acceptance "
        "criteria.",
        schema=IdeaBatch,
    )


@step
async def risk_review(pm2, batch: IdeaBatch) -> PMVerdict:
    return await pm2(
        f"You are a senior PM doing risk analysis on these ideas: "
        f"{batch.model_dump()}. For each, rate risk (low/medium/high) "
        "considering: scope creep, test-by-agent feasibility, blast radius "
        "on the existing codebase, dependency surprises. Pick the LOWEST "
        "risk viable idea. If ALL are high risk, return chosen_index=null "
        "to trigger re-brainstorm.",
        schema=PMVerdict,
    )


@step
async def pick_feature(pm, pm2) -> FeatureIdea:
    for attempt in range(MAX_LOOPS):
        await print(f"[pm] brainstorm round {attempt + 1}")
        batch = await brainstorm(pm)
        verdict = await risk_review(pm2, batch)
        await print(f"[pm] verdict: {verdict.reason}")
        if verdict.chosen_index is not None:
            return batch.ideas[verdict.chosen_index]
        await print("[pm] all ideas high-risk, re-brainstorming")
    raise WorkflowFail("PM could not agree on a low-risk idea")


@step
async def create_ticket(pm, idea: FeatureIdea) -> TicketRef:
    return await pm(
        f"Create a beads ticket for this feature (use `bd create`). "
        f"Title: {idea.title!r}. Description must include the Problem, "
        f"Solution, and non-technical Acceptance Criteria. Use "
        f"`--acceptance` for criteria and `--type=feature --priority=2`. "
        f"Feature details: {idea.model_dump()}. Return the new bd id.",
        schema=TicketRef,
    )


@step
async def draft_plan(architect, ticket_id: str, prior_feedback: list[str]) -> str:
    prompt = (
        f"You are the Architect. Read beads ticket {ticket_id} "
        f"(`bd show {ticket_id}`). Read the relevant godel-py source "
        "(godel/, tests/). Produce an implementation plan: files to "
        "touch, new modules, public API, test plan, migration notes. "
        f"Save plan to the ticket via `bd update {ticket_id} --design=...`."
    )
    if prior_feedback:
        prompt += f"\n\nReviewer required these changes: {prior_feedback}"
    return await architect(prompt)


@step
async def review_plan(reviewer, ticket_id: str) -> PlanReview:
    return await reviewer(
        f"You are a senior engineer reviewing the plan in beads ticket "
        f"{ticket_id} (`bd show {ticket_id}`). Check: (1) FIRST-PRINCIPLES "
        "coherence — does this plan actually solve the PM problem, or does "
        "it drift? (2) technical risks — concurrency, backwards-compat, "
        "test coverage, hidden coupling. Be adversarial but specific. "
        "Approve only if both checks pass.",
        schema=PlanReview,
    )


@step
async def refine_plan(architect, reviewer, ticket_id: str):
    feedback: list[str] = []
    for attempt in range(MAX_LOOPS):
        await print(f"[plan] round {attempt + 1}")
        await draft_plan(architect, ticket_id, feedback)
        review = await review_plan(reviewer, ticket_id)
        if review.approved:
            await print("[plan] approved")
            return
        feedback = review.required_changes + [
            f"coherence: {c}" for c in review.coherence_issues
        ] + [f"risk: {r}" for r in review.technical_risks]
        await print(f"[plan] changes requested: {len(feedback)}")
    raise WorkflowFail(f"plan not approved after {MAX_LOOPS} rounds")


@step
async def implement(engineer, ticket_id: str, prior_fail: list[str]) -> ImplementResult:
    prompt = (
        f"You are a fresh Engineer. Read beads ticket {ticket_id} fully "
        f"(`bd show {ticket_id}`) including the approved plan in the "
        "design field. Claim it (`bd update --claim`). Create a branch "
        f"feat/{ticket_id.lower()}, implement the plan, add tests, run "
        "`pytest` until green, commit with conventional-commit message, "
        "push the branch. Return branch, HEAD sha, files changed, and a "
        "short summary."
    )
    if prior_fail:
        prompt += f"\n\nPrior review/acceptance failures to fix: {prior_fail}"
    return await engineer(prompt, schema=ImplementResult)


@step
async def acceptance_test(pm, ticket_id: str, impl: ImplementResult) -> AcceptanceReport:
    return await pm(
        f"You are the PM running ADVERSARIAL acceptance testing on branch "
        f"{impl.branch}. Checkout the branch. Read acceptance criteria "
        f"from `bd show {ticket_id}`. For EACH criterion, design a probe "
        "that could falsify it (edge cases, empty inputs, concurrent use, "
        "malformed config). Execute the probes. Do NOT trust the engineer's "
        "self-report. Report pass only if every criterion survives probing.",
        schema=AcceptanceReport,
    )


@step
async def code_review(reviewer, ticket_id: str, impl: ImplementResult) -> CodeReviewReport:
    return await reviewer(
        f"You are a senior engineer reviewing branch {impl.branch} for "
        f"ticket {ticket_id}. Check: correctness, test quality, "
        "readability, security, perf hot paths, style coherence with the "
        "rest of godel-py. Block only on substantive issues; nits go in "
        "the nits list.",
        schema=CodeReviewReport,
    )


@step
async def deliver(engineer, ticket_id: str) -> str:
    return await engineer(
        f"Tidy up ticket {ticket_id}: ensure working tree clean, no "
        "stray debug prints, rebase on master if needed, push final "
        f"state, mark beads issue closed (`bd close {ticket_id}`), and "
        "return a human-readable summary of what was built and why."
    )


@workflow
async def feature_factory():
    pm_brain = claude_code(model="sonnet", skip_permissions=True)
    pm_risk = claude_code(model="opus", skip_permissions=True)
    architect = claude_code(model="opus", skip_permissions=True)
    plan_reviewer = claude_code(model="opus", skip_permissions=True)
    engineer = claude_code(model="sonnet", skip_permissions=True)
    acceptance_pm = claude_code(model="sonnet", skip_permissions=True)
    code_reviewer = claude_code(model="opus", skip_permissions=True)

    await print("[factory] phase 1: PM ideation")
    idea = await pick_feature(pm_brain, pm_risk)
    await print(f"[factory] chosen: {idea.title}")
    await input("Checkpoint 1 — approve chosen feature? (enter to continue)")

    await print("[factory] phase 2: ticket creation")
    ticket = await create_ticket(pm_brain, idea)
    await print(f"[factory] ticket {ticket.id} created")

    await print("[factory] phase 3: plan refinement")
    await refine_plan(architect, plan_reviewer, ticket.id)
    await input(f"Checkpoint 2 — review plan in `bd show {ticket.id}` (enter to continue)")

    await print("[factory] phase 4: implementation + review loop")
    prior_fail: list[str] = []
    for attempt in range(MAX_LOOPS):
        await print(f"[factory] implementation round {attempt + 1}")
        impl = await implement(engineer, ticket.id, prior_fail)
        acceptance, review = await parallel(
            acceptance_test(acceptance_pm, ticket.id, impl),
            code_review(code_reviewer, ticket.id, impl),
        )
        if acceptance.passed and review.approved:
            await print("[factory] both reviewers approved")
            break
        prior_fail = acceptance.failures + review.blocking_issues
        await print(f"[factory] loop back, {len(prior_fail)} issues")
    else:
        raise WorkflowFail(f"implementation not approved after {MAX_LOOPS} rounds")

    await input(f"Checkpoint 3 — approve merge for {ticket.id}? (enter to continue)")
    summary = await deliver(engineer, ticket.id)
    await print(f"[factory] done\n\n=== SUMMARY ===\n{summary}")
    return {"ticket": ticket.id, "branch": impl.branch, "summary": summary}

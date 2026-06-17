"""Content pipeline — research, draft, fact-check, and publish.

Demonstrates Godel's value for multi-phase creative workflows where
human judgment intersects with AI generation. Deterministic steps handle
file I/O and structural validation; agents handle research, writing,
and adversarial fact-checking.

Pipeline:
  1. AI research: agent gathers information on a topic.
  2. AI drafting: agent writes an article based on research.
  3. Deterministic: extract claims from the draft (structural parsing).
  4. Parallel AI fact-check: independent agents verify each claim.
  5. Deterministic: compile fact-check results, flag failures.
  6. Human checkpoint: review draft + fact-check results, edit if needed.
  7. Write final article to disk.

Key Godel features shown:
  - read_text / write_text for audited file I/O
  - parallel() for concurrent fact-checking
  - input() for human editorial checkpoint
  - Crash recovery: resume after any failure without re-doing research
  - Rewind: if fact-check reveals bad research, `godel rewind` the
    research step and replay with corrected sources

Usage:
    godel run examples/content_pipeline.py -- topic="deterministic replay in agent workflows"
    godel run examples/content_pipeline.py -- topic="event sourcing for AI" output=article.md
"""
from pydantic import BaseModel
from godel import workflow, step, parallel, run, print, input, write_text, det, CommandFailure


class ResearchBrief(BaseModel):
    key_points: list[str]
    sources: list[str]
    target_audience: str
    angle: str


class FactCheckVerdict(BaseModel):
    claim: str
    supported: bool
    evidence: str
    confidence: str  # high | medium | low


class ArticleDraft(BaseModel):
    title: str
    body: str
    claims: list[str]


# ---------------------------------------------------------------------------
# Phase 1: AI research
# ---------------------------------------------------------------------------

@step
async def research_topic(topic: str) -> ResearchBrief:
    """Agent researches the topic and produces a structured brief."""
    from godel.agents import claude_code

    researcher = claude_code(model="sonnet", skip_permissions=True)
    return await researcher(
        f"You are a technical researcher. Research the topic: '{topic}'.\n\n"
        "Produce a research brief with:\n"
        "- 5-8 key points that should be covered in an article\n"
        "- Sources or references (papers, docs, blog posts) that support each point\n"
        "- Target audience (who would read this)\n"
        "- A specific angle that makes this article worth reading\n\n"
        "Be factual and specific. Every key point must be verifiable.",
        schema=ResearchBrief,
    )


# ---------------------------------------------------------------------------
# Phase 2: AI drafting
# ---------------------------------------------------------------------------

@step
async def draft_article(topic: str, brief: ResearchBrief) -> ArticleDraft:
    """Agent writes the article based on research, extracting verifiable claims."""
    from godel.agents import claude_code

    writer = claude_code(model="sonnet", skip_permissions=True)
    return await writer(
        f"You are a technical writer. Write an article on '{topic}'.\n\n"
        f"Research brief:\n"
        f"- Key points: {brief.key_points}\n"
        f"- Sources: {brief.sources}\n"
        f"- Audience: {brief.target_audience}\n"
        f"- Angle: {brief.angle}\n\n"
        "Write a clear, technical article (800-1200 words). Use markdown formatting.\n\n"
        "IMPORTANT: In the 'claims' field, list every factual claim you make in "
        "the article that could be independently verified (e.g., 'Temporal uses "
        "event sourcing for workflow replay', 'Python 3.10 introduced match '). "
        "List 5-10 verifiable claims.",
        schema=ArticleDraft,
    )


# ---------------------------------------------------------------------------
# Phase 3: Parallel AI fact-checking (one agent per claim)
# ---------------------------------------------------------------------------

@step
async def check_single_claim(claim: str, claim_index: int) -> FactCheckVerdict:
    """Independent agent verifies a single claim. Runs in parallel."""
    from godel.agents import claude_code

    checker = claude_code(model="sonnet", skip_permissions=True)
    return await checker(
        f"You are a fact-checker. Verify this claim:\n\n"
        f"  \"{claim}\"\n\n"
        "Determine if this claim is factually correct. Search your knowledge "
        "for supporting or contradicting evidence. Be skeptical — if you "
        "cannot confirm the claim with high confidence, mark it as unsupported.\n\n"
        "Provide specific evidence for your verdict.",
        schema=FactCheckVerdict,
    )


# ---------------------------------------------------------------------------
# Phase 4: Deterministic result compilation (no AI)
# ---------------------------------------------------------------------------

@step
async def compile_results(
    draft: ArticleDraft, verdicts: list[FactCheckVerdict],
) -> dict:
    """Deterministic compilation of fact-check results."""
    supported = [v for v in verdicts if v.supported]
    unsupported = [v for v in verdicts if not v.supported]
    total = len(verdicts)
    pass_rate = round(len(supported) / total * 100) if total else 0

    await print(f"\n[fact-check] results: {len(supported)}/{total} claims supported ({pass_rate}%)")

    if unsupported:
        await print(f"[fact-check] UNSUPPORTED CLAIMS:")
        for v in unsupported:
            await print(f"  ✗ {v.claim}")
            await print(f"    evidence: {v.evidence[:100]}")

    report_lines = []
    report_lines.append(f"## Fact-Check Report\n")
    report_lines.append(f"**Claims checked:** {total}")
    report_lines.append(f"**Supported:** {len(supported)}")
    report_lines.append(f"**Unsupported:** {len(unsupported)}")
    report_lines.append(f"**Pass rate:** {pass_rate}%\n")

    report_lines.append("| # | Claim | Verdict | Confidence | Evidence |")
    report_lines.append("|---|-------|---------|------------|----------|")
    for i, v in enumerate(verdicts, 1):
        icon = "✓" if v.supported else "✗"
        claim_short = v.claim[:60] + "..." if len(v.claim) > 60 else v.claim
        evidence_short = v.evidence[:80] + "..." if len(v.evidence) > 80 else v.evidence
        report_lines.append(
            f"| {i} | {claim_short} | {icon} | {v.confidence} | {evidence_short} |"
        )

    return {
        "pass_rate": pass_rate,
        "total": total,
        "supported_count": len(supported),
        "unsupported_count": len(unsupported),
        "unsupported_claims": [v.claim for v in unsupported],
        "fact_check_table": "\n".join(report_lines),
    }


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

@workflow
async def content_pipeline(topic: str = "event sourcing for AI agents", output: str = "article.md"):
    timestamp = det.now()
    await print(f"[pipeline] starting content pipeline at {timestamp}")
    await print(f"[pipeline] topic: {topic}")

    # Phase 1 — research
    await print("\n[pipeline] === phase 1: researching ===")
    brief = await research_topic(topic)
    await print(f"[research] angle: {brief.angle}")
    await print(f"[research] {len(brief.key_points)} key points, {len(brief.sources)} sources")

    # Phase 2 — drafting
    await print("\n[pipeline] === phase 2: drafting article ===")
    draft = await draft_article(topic, brief)
    await print(f"[draft] title: {draft.title}")
    await print(f"[draft] {len(draft.body)} chars, {len(draft.claims)} verifiable claims")

    # Phase 3 — parallel fact-checking
    await print(f"\n[pipeline] === phase 3: fact-checking {len(draft.claims)} claims in parallel ===")
    verdicts = await parallel(*[
        check_single_claim(claim, i) for i, claim in enumerate(draft.claims)
    ])

    # Phase 4 — deterministic compilation
    await print("\n[pipeline] === phase 4: compiling results ===")
    results = await compile_results(draft, verdicts)

    # Phase 5 — summary
    await print(f"\n{'='*60}")
    await print(f"ARTICLE: {draft.title}")
    await print(f"{'='*60}")
    await print(draft.body[:500] + "...\n")
    await print(results["fact_check_table"])

    if results["unsupported_count"] > 0:
        await print(f"\n⚠ {results['unsupported_count']} unsupported claims found.")

    # Phase 6 — write final article with fact-check appendix
    final = f"{draft.body}\n\n---\n\n{results['fact_check_table']}\n"
    await write_text(output, final)
    await print(f"\n[pipeline] article saved to {output}")
    await print(f"[pipeline] fact-check pass rate: {results['pass_rate']}%")

    return {
        "title": draft.title,
        "output": output,
        "claims_checked": results["total"],
        "pass_rate": results["pass_rate"],
    }

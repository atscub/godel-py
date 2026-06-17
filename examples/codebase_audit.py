"""Codebase audit — multi-axis quality assessment combining deterministic tools with AI judgment.

Demonstrates Godel's core philosophy: deterministic orchestration handles structure
(file discovery, metric collection, parallel coordination), while agents handle
judgment (interpreting findings, assessing severity, writing recommendations).

Pipeline:
  1. Deterministic: collect project metrics via shell tools (loc, test count,
     dependency count, lint output, git activity).
  2. Parallel AI analysis: five independent agents each audit one quality axis
     using the metrics as grounding data.
  3. Deterministic: merge structured verdicts, compute aggregate score.
  4. AI synthesis: one agent writes the final narrative report from the verdicts.
  5. Human checkpoint: review before writing report to disk.
  6. Write report to file (audited, replayable).

Crash at any point — `godel resume` picks up without re-running completed steps.
Disagree with a verdict — `godel rewind` that step and replay with new context.

Usage:
    godel run examples/codebase_audit.py -- path=./my-project
    godel run examples/codebase_audit.py -- path=./my-project report=audit.md
"""
from pydantic import BaseModel
from godel import workflow, step, parallel, run, print, input, write_text, det, CommandFailure


AUDIT_AXES = [
    {
        "axis": "test_quality",
        "label": "Test Quality",
        "focus": (
            "Test coverage strategy, test naming, assertion quality, edge case "
            "coverage, test isolation, fixture reuse. Are tests testing behavior "
            "or implementation details?"
        ),
    },
    {
        "axis": "code_consistency",
        "label": "Code Consistency",
        "focus": (
            "Naming conventions, module structure, import style, error handling "
            "patterns, logging patterns. Is there one way to do things or many "
            "contradictory patterns?"
        ),
    },
    {
        "axis": "security",
        "label": "Security",
        "focus": (
            "Input validation, injection vectors (SQL, command, path traversal), "
            "secret handling, dependency vulnerabilities, auth patterns. Focus on "
            "OWASP top 10 relevant to this project type."
        ),
    },
    {
        "axis": "architecture",
        "label": "Architecture & Modularity",
        "focus": (
            "Separation of concerns, dependency direction, circular imports, "
            "public API surface clarity, coupling between modules. Could a new "
            "contributor understand the boundaries?"
        ),
    },
    {
        "axis": "documentation",
        "label": "Documentation & DX",
        "focus": (
            "README quality, docstring coverage on public API, example quality, "
            "error message helpfulness, CLI help text. Could someone adopt this "
            "project from the docs alone?"
        ),
    },
]


class AxisVerdict(BaseModel):
    axis: str
    score: int  # 1-10
    summary: str
    strengths: list[str]
    issues: list[str]
    recommendations: list[str]


# ---------------------------------------------------------------------------
# Phase 1: Deterministic metric collection (no AI, pure shell tools)
# ---------------------------------------------------------------------------

@step
async def collect_metrics(project_path: str) -> dict:
    """Gather raw metrics using standard CLI tools. Fully deterministic."""
    await print(f"[metrics] scanning {project_path}")

    loc = await run(
        "find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' -exec cat {} +",
        cwd=project_path,
    )
    line_count = len(loc.stdout.splitlines())

    file_list = await run(
        "find . -name '*.py' -not -path './.venv/*' -not -path './.git/*'",
        cwd=project_path,
    )
    files = [f for f in file_list.stdout.strip().splitlines() if f]

    test_files = [f for f in files if "/test_" in f or f.endswith("_test.py")]

    try:
        deps = await run("pip list --format=columns", cwd=project_path)
        dep_count = max(0, len(deps.stdout.strip().splitlines()) - 2)
    except CommandFailure:
        dep_count = -1

    try:
        lint = await run("ruff check . --statistics --quiet", cwd=project_path)
        lint_output = lint.stdout.strip()
    except CommandFailure as e:
        lint_output = e.stderr.strip() if e.stderr else e.stdout.strip() if e.stdout else "(ruff not available)"

    try:
        git_log = await run("git log --oneline -20", cwd=project_path)
        recent_commits = git_log.stdout.strip()
    except CommandFailure:
        recent_commits = "(not a git repo)"

    metrics = {
        "python_lines": line_count,
        "python_files": len(files),
        "test_files": len(test_files),
        "dependency_count": dep_count,
        "lint_output": lint_output,
        "recent_commits": recent_commits,
        "file_tree": "\n".join(files[:100]),
    }
    await print(
        f"[metrics] {line_count} lines, {len(files)} files, "
        f"{len(test_files)} test files, {dep_count} deps"
    )
    return metrics


# ---------------------------------------------------------------------------
# Phase 2: Parallel AI audits — one agent per axis, all independent
# ---------------------------------------------------------------------------

@step
async def audit_axis(axis_config: dict, metrics: dict, project_path: str) -> AxisVerdict:
    """One agent audits one quality axis, grounded in collected metrics."""
    from godel.agents import claude_code

    auditor = claude_code(model="sonnet", skip_permissions=True, cwd=project_path)
    return await auditor(
        f"You are a senior engineer auditing a Python project on the axis: "
        f"**{axis_config['label']}**.\n\n"
        f"Focus area: {axis_config['focus']}\n\n"
        f"Project metrics (collected by deterministic tools, not by you):\n"
        f"- Python lines: {metrics['python_lines']}\n"
        f"- Python files: {metrics['python_files']}\n"
        f"- Test files: {metrics['test_files']}\n"
        f"- Dependencies: {metrics['dependency_count']}\n"
        f"- Lint output:\n{metrics['lint_output']}\n"
        f"- Recent commits:\n{metrics['recent_commits']}\n"
        f"- File tree (first 100):\n{metrics['file_tree']}\n\n"
        "Read the source code yourself to go deeper. Score 1-10 (10 = excellent). "
        "Be specific: cite file names and line numbers in issues and strengths. "
        "Keep recommendations actionable — what exactly should change.",
        schema=AxisVerdict,
    )


# ---------------------------------------------------------------------------
# Phase 3: Deterministic report assembly (no AI)
# ---------------------------------------------------------------------------

def _score_bar(score: int, max_score: int = 10) -> str:
    filled = score
    empty = max_score - score
    return f"{'█' * filled}{'░' * empty}"


@step
async def build_structured_report(
    verdicts: list[AxisVerdict], metrics: dict, timestamp: str,
) -> dict:
    """Assemble the data-driven sections of the report. Pure deterministic logic."""
    total = sum(v.score for v in verdicts)
    max_possible = len(verdicts) * 10
    pct = round(total / max_possible * 100)
    sorted_v = sorted(verdicts, key=lambda v: v.score)

    lines = []

    lines.append("# Codebase Audit Report\n")
    lines.append(f"**Date:** {timestamp}  ")
    lines.append(f"**Overall Score: {total}/{max_possible} ({pct}%)**\n")

    lines.append("## Project Metrics\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Python lines | {metrics['python_lines']:,} |")
    lines.append(f"| Python files | {metrics['python_files']} |")
    lines.append(f"| Test files | {metrics['test_files']} |")
    lines.append(f"| Dependencies | {metrics['dependency_count']} |")
    lines.append("")

    lines.append("## Score Summary\n")
    lines.append("| Axis | Score | |")
    lines.append("|------|-------|-|")
    for v in sorted_v:
        label = next(
            (a["label"] for a in AUDIT_AXES if a["axis"] == v.axis),
            v.axis,
        )
        lines.append(f"| {label} | **{v.score}/10** | {_score_bar(v.score)} |")
    lines.append("")

    lines.append("---\n")
    lines.append("## Executive Summary\n")
    lines.append("{{EXECUTIVE_SUMMARY}}\n")
    lines.append("---\n")

    for v in sorted_v:
        label = next(
            (a["label"] for a in AUDIT_AXES if a["axis"] == v.axis),
            v.axis,
        )
        lines.append(f"## {label} — {v.score}/10\n")
        lines.append(f"_{v.summary}_\n")

        if v.strengths:
            lines.append("### Strengths\n")
            for s in v.strengths:
                lines.append(f"- {s}")
            lines.append("")

        if v.issues:
            lines.append("### Issues\n")
            for i, issue in enumerate(v.issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        if v.recommendations:
            lines.append("### Recommendations\n")
            for r in v.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        lines.append("---\n")

    all_issues = []
    for v in sorted_v:
        for issue in v.issues:
            all_issues.append((v.axis, issue))

    if all_issues:
        lines.append("## All Issues\n")
        lines.append("| # | Axis | Issue |")
        lines.append("|---|------|-------|")
        for i, (axis, issue) in enumerate(all_issues, 1):
            label = next(
                (a["label"] for a in AUDIT_AXES if a["axis"] == axis),
                axis,
            )
            issue_escaped = issue.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {i} | {label} | {issue_escaped} |")
        lines.append("")

    if metrics.get("lint_output") and metrics["lint_output"] != "(ruff not available)":
        lines.append("## Lint Output\n")
        lines.append("```")
        lines.append(metrics["lint_output"])
        lines.append("```\n")

    structured = "\n".join(lines)

    await print(
        f"[aggregate] overall: {total}/{max_possible} ({pct}%) | "
        f"weakest: {sorted_v[0].axis} ({sorted_v[0].score}) | "
        f"strongest: {sorted_v[-1].axis} ({sorted_v[-1].score})"
    )

    return {
        "total_score": total,
        "max_score": max_possible,
        "percentage": pct,
        "weakest_axis": sorted_v[0].axis,
        "strongest_axis": sorted_v[-1].axis,
        "structured_report": structured,
        "all_issues_count": len(all_issues),
    }


# ---------------------------------------------------------------------------
# Phase 4: AI executive summary only
# ---------------------------------------------------------------------------

@step
async def write_executive_summary(verdicts: list[AxisVerdict], metrics: dict) -> str:
    """AI writes only the executive summary — everything else is data-driven."""
    from godel.agents import claude_code

    verdict_summary = "\n".join(
        f"- {v.axis} ({v.score}/10): {v.summary}" for v in verdicts
    )
    issue_summary = "\n".join(
        f"- [{v.axis}] {issue}"
        for v in verdicts
        for issue in v.issues
    )

    writer = claude_code(model="sonnet", skip_permissions=True)
    return await writer(
        f"Write an executive summary (3-5 sentences) for a codebase audit.\n\n"
        f"Project metrics: {metrics['python_lines']} lines, "
        f"{metrics['python_files']} files, {metrics['test_files']} test files.\n\n"
        f"Axis scores:\n{verdict_summary}\n\n"
        f"Key issues found:\n{issue_summary}\n\n"
        "Be direct and specific. Mention the most critical findings by name. "
        "State what must be fixed before the project can be trusted. "
        "Output ONLY the summary paragraph(s), no headers or markdown formatting."
    )


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

@workflow
async def codebase_audit(path: str = ".", report: str = "audit-report.md"):
    timestamp = det.now()
    await print(f"[audit] starting codebase audit at {timestamp}")
    await print(f"[audit] target: {path}")

    # Phase 1 — deterministic metric collection
    await print("\n[audit] === phase 1: collecting metrics ===")
    metrics = await collect_metrics(path)

    # Phase 2 — parallel AI audits (all axes run concurrently)
    await print("\n[audit] === phase 2: parallel audits (5 axes) ===")
    verdicts = await parallel(*[
        audit_axis(axis, metrics, path) for axis in AUDIT_AXES
    ])
    for v in verdicts:
        await print(f"  [{v.axis}] {v.score}/10 — {v.summary[:80]}")

    # Phase 3 — deterministic report assembly + AI executive summary (parallel)
    await print("\n[audit] === phase 3: assembling report ===")
    aggregate, executive = await parallel(
        build_structured_report(verdicts, metrics, timestamp),
        write_executive_summary(verdicts, metrics),
    )

    # Phase 4 — combine structured report with AI summary
    final_report = aggregate["structured_report"].replace(
        "{{EXECUTIVE_SUMMARY}}", executive
    )

    # Phase 5 — human checkpoint
    await print(f"\n[audit] report preview:\n{final_report[:800]}...")
    await input("\nCheckpoint — review above. Press enter to save, or Ctrl+C to abort.")

    # Phase 6 — audited file write
    await write_text(report, final_report)
    await print(f"\n[audit] report saved to {report}")
    await print(f"[audit] overall score: {aggregate['percentage']}%")

    return {
        "score": aggregate["percentage"],
        "report_path": report,
        "weakest": aggregate["weakest_axis"],
        "strongest": aggregate["strongest_axis"],
        "issues_found": aggregate["all_issues_count"],
    }

"""Incident response runbook — structured diagnosis and remediation.

Demonstrates how Godel turns a runbook into an auditable, resumable process.
Deterministic steps gather diagnostics (logs, metrics, recent deploys);
agents interpret findings and propose fixes; human gates prevent
unsupervised changes to production.

Pipeline:
  1. Deterministic: gather diagnostics in parallel (logs, metrics, deploys).
  2. AI triage: agent analyzes all diagnostics, classifies severity, proposes fix.
  3. Human gate: operator approves or rejects the proposed fix.
  4. AI remediation: agent executes the approved fix.
  5. Deterministic: verify the fix with health checks (with retry).
  6. AI postmortem: agent writes a summary for the incident log.

If the workflow crashes mid-incident, `godel resume` picks up from the last
completed step — no re-gathering diagnostics, no re-running the fix.
`godel show` provides a full audit trail of every action taken.

Usage:
    godel run examples/incident_response.py -- service=api-gateway alert="high error rate on /v1/users"
"""
from pydantic import BaseModel
from godel import workflow, step, parallel, retry, run, print, input, write_text, det, WorkflowFail, CommandFailure


class Diagnostics(BaseModel):
    logs: str
    metrics: str
    recent_deploys: str
    service_status: str


class TriageResult(BaseModel):
    severity: str  # critical | high | medium | low
    root_cause: str
    affected_components: list[str]
    proposed_fix: str
    rollback_needed: bool
    estimated_impact: str


class RemediationResult(BaseModel):
    actions_taken: list[str]
    rollback_performed: bool
    notes: str


# ---------------------------------------------------------------------------
# Phase 1: Deterministic diagnostic collection (parallel, no AI)
# ---------------------------------------------------------------------------

@step
async def gather_logs(service: str) -> str:
    """Pull recent error logs. Deterministic shell command."""
    try:
        result = await run(
            "printf '%s\\n' "
            "'14:23:01 ERROR connection pool exhausted (5 occurrences)' "
            "'14:23:05 ERROR upstream timeout after 30s' "
            "'14:23:12 WARN circuit breaker open for db-primary' "
            "'14:22:58 ERROR OOM killed worker pid=4521' "
            "'14:22:55 ERROR connection pool exhausted (3 occurrences)' "
            "'14:22:50 WARN memory pressure above 90%% on worker-3' "
            "'14:22:44 ERROR upstream timeout after 30s' "
            "'14:22:30 INFO worker pid=4521 restarted after OOM'",
        )
        return result.stdout.strip()
    except CommandFailure as e:
        return e.stdout.strip() if e.stdout else "(no logs available)"


@step
async def gather_metrics(service: str) -> str:
    """Pull key metrics. In production this would query Prometheus/Datadog."""
    try:
        result = await run(
            "printf '%s\\n' "
            f"'[simulated metrics for {service}]' "
            "'error_rate: 23.4% (threshold: 1%)' "
            "'p99_latency: 4200ms (threshold: 500ms)' "
            "'active_connections: 847 (limit: 500)' "
            "'memory_usage: 94.2% (threshold: 80%)' "
            "'cpu_usage: 67.3%' "
            "'healthy_replicas: 2/5'",
        )
        return result.stdout.strip()
    except CommandFailure as e:
        return "(metrics unavailable)"


@step
async def gather_deploys(service: str) -> str:
    """Check recent deployments. In production this would query CI/CD."""
    try:
        result = await run(
            "printf '%s\\n' "
            f"'[recent deploys for {service}]' "
            "'2h ago  v2.14.3  deploy by CI  fix: connection pool sizing' "
            "'1d ago  v2.14.2  deploy by CI  feat: add batch endpoint' "
            "'3d ago  v2.14.1  deploy by CI  chore: dependency updates'",
        )
        return result.stdout.strip()
    except CommandFailure as e:
        return "(deploy history unavailable)"


@step
async def check_service_status(service: str) -> str:
    """Check current service health."""
    try:
        result = await run(
            "printf '%s\\n' "
            f"'[service status: {service}]' "
            "'replicas: 2/5 healthy' "
            "'last restart: 47 minutes ago' "
            "'uptime before restart: 2h14m' "
            "'pending restarts: 3'",
        )
        return result.stdout.strip()
    except CommandFailure as e:
        return "(status check failed)"


@step
async def collect_diagnostics(service: str) -> Diagnostics:
    """Gather all diagnostics in parallel — no AI, pure shell commands."""
    await print(f"[diagnostics] gathering data for {service}")
    logs, metrics, deploys, status = await parallel(
        gather_logs(service),
        gather_metrics(service),
        gather_deploys(service),
        check_service_status(service),
    )
    await print(f"[diagnostics] collected logs ({len(logs)} chars), "
                f"metrics, {deploys.count(chr(10))+1} deploys, status")
    return Diagnostics(
        logs=logs, metrics=metrics,
        recent_deploys=deploys, service_status=status,
    )


# ---------------------------------------------------------------------------
# Phase 2: AI triage
# ---------------------------------------------------------------------------

@step
async def triage_incident(service: str, alert: str, diag: Diagnostics) -> TriageResult:
    """Agent analyzes diagnostics and proposes a fix."""
    from godel.agents import claude_code

    analyst = claude_code(model="sonnet", skip_permissions=True)
    return await analyst(
        f"You are an on-call SRE triaging an incident.\n\n"
        f"**Service:** {service}\n"
        f"**Alert:** {alert}\n\n"
        f"**Logs:**\n```\n{diag.logs}\n```\n\n"
        f"**Metrics:**\n```\n{diag.metrics}\n```\n\n"
        f"**Recent deploys:**\n```\n{diag.recent_deploys}\n```\n\n"
        f"**Service status:**\n```\n{diag.service_status}\n```\n\n"
        "Analyze the diagnostics. Determine root cause, severity, and propose "
        "a specific fix. If the most recent deploy correlates with the issue, "
        "recommend rollback. Be precise — cite specific log lines and metrics.",
        schema=TriageResult,
    )


# ---------------------------------------------------------------------------
# Phase 3: AI remediation (after human approval)
# ---------------------------------------------------------------------------

@step
async def execute_remediation(service: str, triage: TriageResult) -> RemediationResult:
    """Agent executes the approved fix."""
    from godel.agents import claude_code

    engineer = claude_code(model="sonnet", skip_permissions=True)
    return await engineer(
        f"You are an SRE executing an approved remediation plan.\n\n"
        f"**Service:** {service}\n"
        f"**Root cause:** {triage.root_cause}\n"
        f"**Approved fix:** {triage.proposed_fix}\n"
        f"**Rollback needed:** {triage.rollback_needed}\n\n"
        "Execute the fix. Since this is a demo environment, describe the exact "
        "commands you would run (don't actually run destructive commands). "
        "List each action taken. Note any complications.",
        schema=RemediationResult,
    )


# ---------------------------------------------------------------------------
# Phase 4: Deterministic health check (with retry)
# ---------------------------------------------------------------------------

@retry(3, backoff_seconds=2.0)
@step
async def verify_health(service: str) -> str:
    """Check that the service recovered. Retries up to 3 times with backoff."""
    result = await run(
        f"echo '[health check: {service}] status=healthy replicas=5/5 error_rate=0.02%'"
    )
    output = result.stdout.strip()
    if "status=healthy" not in output:
        raise WorkflowFail(f"Health check failed: {output}")
    await print(f"[health] {service} is healthy")
    return output


# ---------------------------------------------------------------------------
# Phase 5: AI postmortem
# ---------------------------------------------------------------------------

@step
async def write_postmortem(
    service: str, alert: str, diag: Diagnostics,
    triage: TriageResult, remediation: RemediationResult,
    health: str, timestamp: str,
) -> str:
    """Agent writes a structured postmortem."""
    from godel.agents import claude_code

    writer = claude_code(model="sonnet", skip_permissions=True)
    return await writer(
        f"Write a concise incident postmortem in markdown.\n\n"
        f"**Incident date:** {timestamp}\n"
        f"**Service:** {service}\n"
        f"**Alert:** {alert}\n"
        f"**Severity:** {triage.severity}\n"
        f"**Root cause:** {triage.root_cause}\n"
        f"**Affected components:** {', '.join(triage.affected_components)}\n"
        f"**Actions taken:** {', '.join(remediation.actions_taken)}\n"
        f"**Rollback performed:** {remediation.rollback_performed}\n"
        f"**Resolution verified:** {health}\n\n"
        "Structure: Timeline, Root Cause, Impact, Resolution, Lessons Learned, "
        "Action Items. Be specific and factual. Output ONLY the markdown."
    )


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

@workflow
async def incident_response(service: str = "api-gateway", alert: str = "high error rate"):
    timestamp = det.now()
    await print(f"[incident] === INCIDENT RESPONSE INITIATED ===")
    await print(f"[incident] service: {service}")
    await print(f"[incident] alert: {alert}")
    await print(f"[incident] time: {timestamp}")

    # Phase 1 — parallel diagnostic collection (deterministic)
    await print("\n[incident] === phase 1: gathering diagnostics ===")
    diag = await collect_diagnostics(service)

    # Phase 2 — AI triage
    await print("\n[incident] === phase 2: triaging ===")
    triage = await triage_incident(service, alert, diag)
    await print(f"[triage] severity: {triage.severity}")
    await print(f"[triage] root cause: {triage.root_cause}")
    await print(f"[triage] proposed fix: {triage.proposed_fix}")
    await print(f"[triage] rollback needed: {triage.rollback_needed}")

    # Phase 3 — human approval gate
    await print("\n[incident] === phase 3: awaiting approval ===")
    await print(
        f"\n[APPROVAL REQUIRED]\n"
        f"Severity: {triage.severity}\n"
        f"Proposed fix: {triage.proposed_fix}\n"
        f"Rollback: {'yes' if triage.rollback_needed else 'no'}"
    )
    await input("Press enter to approve, or Ctrl+C to abort.")

    # Phase 4 — execute remediation
    await print("\n[incident] === phase 4: executing fix ===")
    remediation = await execute_remediation(service, triage)
    for action in remediation.actions_taken:
        await print(f"  [action] {action}")

    # Phase 5 — verify health (with retry)
    await print("\n[incident] === phase 5: verifying health ===")
    health = await verify_health(service)

    # Phase 6 — write postmortem
    await print("\n[incident] === phase 6: writing postmortem ===")
    postmortem = await write_postmortem(
        service, alert, diag, triage, remediation, health, timestamp,
    )
    report_path = f"incident-{service}-{timestamp[:10]}.md"
    await write_text(report_path, postmortem)
    await print(f"[incident] postmortem saved to {report_path}")

    await print("\n[incident] === INCIDENT RESOLVED ===")
    return {
        "severity": triage.severity,
        "root_cause": triage.root_cause,
        "actions_taken": len(remediation.actions_taken),
        "report": report_path,
    }

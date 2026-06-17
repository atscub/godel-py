# Examples

Non-trivial workflows demonstrating Godel's capabilities. Each example is tested end-to-end with real data and real agent calls.

**Prerequisites:** Install Godel and have the `claude` CLI available.

```bash
pip install godel-py
```

---

## Codebase Audit

Multi-axis code quality assessment combining deterministic tools (LOC, lint, git log) with parallel AI judgment across 5 axes: test quality, consistency, security, architecture, and documentation.

**Features shown:** `parallel`, `run()`, structured schemas, `det.now()`, `write_text`, `input`, deterministic vs AI separation.

```bash
git clone https://github.com/atscub/nautapy /tmp/nautapy
godel run examples/codebase_audit.py -- path=/tmp/nautapy report=audit.md
```

**Expected output:** A markdown report (~250 lines) with metrics table, score summary with visual bars, per-axis breakdown with file:line citations, all-issues table, and lint output. Runtime: ~2 minutes.

---

## Incident Response

Structured incident diagnosis and remediation runbook. Gathers diagnostics in parallel, AI triages and proposes a fix, human approves, agent executes remediation, health check retries until green, then writes a postmortem.

**Features shown:** `parallel`, `retry` with backoff, `input` (human approval gate), `write_text`, `WorkflowFail`, audit trail.

```bash
godel run examples/incident_response.py -- service=api-gateway alert="high error rate on /v1/users"
```

**Expected output:** Interactive — you'll be prompted to approve the proposed fix. Type `approve` to proceed. Produces an incident postmortem markdown file. Runtime: ~2 minutes.

---

## Content Pipeline

Research a topic, draft an article, fact-check every claim in parallel, then publish with human editorial review.

**Features shown:** `parallel` fact-checking, `write_text`, `input` (editorial checkpoint), rewind scenario (if fact-check fails, `godel rewind` the research step).

```bash
godel run examples/content_pipeline.py -- topic="deterministic replay in agent workflows"
```

**Expected output:** Interactive — you'll see the draft preview and fact-check table, then approve to save. Produces a markdown article with fact-check appendix. Runtime: ~3 minutes.

---

## Data Quality

Validate, diagnose, and fix a CSV dataset. Deterministic steps handle schema validation and statistical checks; AI diagnoses anomalies and proposes context-aware fixes; human reviews before changes are applied.

**Features shown:** `read_text`/`write_text`, deterministic CSV validation, AI diagnosis, `input` (approval checkpoint), non-AI-meta use case.

```bash
curl -o /tmp/titanic.csv https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
godel run examples/data_quality.py -- input=/tmp/titanic.csv output=clean_titanic.csv
```

**Expected output:** Interactive — shows quality report (nulls, duplicates, outliers, type issues) and proposed fixes. Approve to write the cleaned CSV. Runtime: ~1 minute.

---

## Feature Factory

End-to-end autonomous feature delivery: PM brainstorms ideas, risk assessment picks one, architect plans, reviewer critiques, engineer implements, parallel acceptance test + code review, loop until approved.

**Features shown:** `parallel`, `input` (3 human checkpoints), `WorkflowFail`, structured schemas, multi-agent orchestration.

```bash
godel run examples/feature_factory.py
```

**Expected output:** Interactive — checkpoints at feature selection, plan review, and merge approval. Produces a complete feature branch with tests. Runtime: ~10-15 minutes.

---

## Resume and Rewind

Every example supports crash recovery and correction:

```bash
# Resume after a crash or Ctrl+C
godel resume <run-id>

# Rewind a specific step (e.g., bad research → redo)
godel show <run-id>              # find the step event ID
godel rewind <run-id> <event-id> # invalidate that step
godel resume <run-id>            # replay from that point
```

The run ID is printed at the start of every workflow. Use `godel show <run-id>` to inspect the full audit trail.

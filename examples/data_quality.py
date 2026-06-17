"""Data quality pipeline — validate, diagnose, and fix a CSV dataset.

A non-AI-meta use case showing Godel orchestrating a data engineering
workflow. Deterministic steps handle schema validation, statistical
checks, and file I/O. Agents handle ambiguous judgment calls:
diagnosing why anomalies exist and proposing context-aware fixes.

Pipeline:
  1. Deterministic: read CSV, validate schema (column names, types).
  2. Deterministic: run statistical checks (nulls, duplicates, outliers).
  3. AI diagnosis: agent interprets anomalies in business context.
  4. AI fixes: agents propose fixes for each category of issue (parallel).
  5. Human checkpoint: review proposed changes.
  6. Deterministic: apply fixes, write clean CSV.

Key Godel features shown:
  - read_text / write_text for audited file I/O
  - Deterministic data validation (no AI needed for schema checks)
  - AI only where judgment is required (interpreting anomalies)
  - parallel() for concurrent fix generation
  - input() for human approval before modifying data
  - Full audit trail: every data transformation is logged

Usage:
    godel run examples/data_quality.py -- input=data/raw.csv output=data/clean.csv
"""
import csv
import io
import json

from pydantic import BaseModel
from godel import workflow, step, parallel, print, input, read_text, write_text, det


class SchemaReport(BaseModel):
    valid: bool
    columns: list[str]
    row_count: int
    errors: list[str]


class QualityReport(BaseModel):
    null_counts: dict[str, int]
    duplicate_rows: int
    outlier_columns: list[str]
    empty_columns: list[str]
    type_violations: list[str]
    summary: str


class FixProposal(BaseModel):
    category: str
    description: str
    affected_rows: int
    action: str  # drop | fill | coerce | flag
    details: str


class FixProposalList(BaseModel):
    proposals: list[FixProposal]


# ---------------------------------------------------------------------------
# Phase 1: Deterministic schema validation (no AI)
# ---------------------------------------------------------------------------

@step
async def validate_schema(raw_csv: str) -> SchemaReport:
    """Parse CSV and validate basic schema. Pure deterministic logic."""
    await print("[schema] validating CSV structure")
    errors = []

    try:
        reader = csv.DictReader(io.StringIO(raw_csv))
        columns = reader.fieldnames or []
        rows = list(reader)
    except csv.Error as e:
        return SchemaReport(
            valid=False, columns=[], row_count=0,
            errors=[f"CSV parse error: {e}"],
        )

    if not columns:
        errors.append("No columns found — file may be empty or malformed")
    if not rows:
        errors.append("No data rows found")

    for col in columns:
        if col.strip() != col:
            errors.append(f"Column '{col}' has leading/trailing whitespace")
        if col == "":
            errors.append("Empty column name found")

    dupes = [c for c in set(columns) if columns.count(c) > 1]
    if dupes:
        errors.append(f"Duplicate column names: {dupes}")

    report = SchemaReport(
        valid=len(errors) == 0,
        columns=list(columns),
        row_count=len(rows),
        errors=errors,
    )
    await print(f"[schema] {report.row_count} rows, {len(report.columns)} columns, "
                f"{len(report.errors)} errors")
    return report


# ---------------------------------------------------------------------------
# Phase 2: Deterministic quality checks (no AI)
# ---------------------------------------------------------------------------

@step
async def run_quality_checks(raw_csv: str, schema: SchemaReport) -> QualityReport:
    """Statistical quality checks. Pure deterministic analysis."""
    await print("[quality] running statistical checks")
    reader = csv.DictReader(io.StringIO(raw_csv))
    rows = list(reader)
    columns = schema.columns

    null_counts = {}
    for col in columns:
        nulls = sum(1 for r in rows if not r.get(col, "").strip())
        if nulls > 0:
            null_counts[col] = nulls

    seen = set()
    duplicate_rows = 0
    for r in rows:
        key = tuple(sorted(r.items()))
        if key in seen:
            duplicate_rows += 1
        seen.add(key)

    empty_columns = [col for col in columns
                     if all(not r.get(col, "").strip() for r in rows)]

    numeric_cols = {}
    for col in columns:
        values = []
        for r in rows:
            try:
                values.append(float(r[col]))
            except (ValueError, TypeError, KeyError):
                pass
        if len(values) > len(rows) * 0.5:
            numeric_cols[col] = values

    outlier_columns = []
    for col, values in numeric_cols.items():
        if len(values) < 4:
            continue
        sorted_vals = sorted(values)
        q1 = sorted_vals[len(sorted_vals) // 4]
        q3 = sorted_vals[3 * len(sorted_vals) // 4]
        iqr = q3 - q1
        if iqr == 0:
            continue
        outliers = [v for v in values if v < q1 - 1.5 * iqr or v > q3 + 1.5 * iqr]
        if len(outliers) > 0:
            outlier_columns.append(f"{col} ({len(outliers)} outliers)")

    type_violations = []
    for col in columns:
        values = [r.get(col, "") for r in rows if r.get(col, "").strip()]
        if not values:
            continue
        num_count = sum(1 for v in values
                        if v.replace(".", "", 1).replace("-", "", 1).isdigit())
        str_count = len(values) - num_count
        if num_count > 0 and str_count > 0 and min(num_count, str_count) > len(values) * 0.1:
            type_violations.append(
                f"{col}: mixed types ({num_count} numeric, {str_count} string)"
            )

    issues_found = (len(null_counts) + (1 if duplicate_rows else 0)
                    + len(outlier_columns) + len(empty_columns) + len(type_violations))
    summary = (
        f"{issues_found} issue categories found: "
        f"{len(null_counts)} columns with nulls, "
        f"{duplicate_rows} duplicate rows, "
        f"{len(outlier_columns)} columns with outliers, "
        f"{len(empty_columns)} empty columns, "
        f"{len(type_violations)} mixed-type columns"
    )

    await print(f"[quality] {summary}")
    return QualityReport(
        null_counts=null_counts,
        duplicate_rows=duplicate_rows,
        outlier_columns=outlier_columns,
        empty_columns=empty_columns,
        type_violations=type_violations,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Phase 3: AI diagnosis (judgment required)
# ---------------------------------------------------------------------------

@step
async def diagnose_issues(
    schema: SchemaReport, quality: QualityReport,
) -> list[FixProposal]:
    """Agent interprets quality issues and proposes context-aware fixes."""
    from godel.agents import claude_code

    analyst = claude_code(model="sonnet", skip_permissions=True)
    proposals = await analyst(
        f"You are a data engineer reviewing quality issues in a CSV dataset.\n\n"
        f"**Schema:** {len(schema.columns)} columns: {schema.columns}\n"
        f"**Rows:** {schema.row_count}\n\n"
        f"**Quality issues found:**\n"
        f"- Null counts by column: {json.dumps(quality.null_counts)}\n"
        f"- Duplicate rows: {quality.duplicate_rows}\n"
        f"- Outlier columns: {quality.outlier_columns}\n"
        f"- Empty columns: {quality.empty_columns}\n"
        f"- Type violations: {quality.type_violations}\n\n"
        "For each issue, propose a fix. Choose the action that preserves "
        "data integrity: 'drop' removes bad rows/columns, 'fill' imputes "
        "values, 'coerce' converts types, 'flag' adds a quality column. "
        "Be conservative — don't drop data unless it's clearly garbage.\n\n"
        "Return a list of fix proposals, one per issue category.",
        schema=FixProposalList,
    )
    return proposals.proposals


# ---------------------------------------------------------------------------
# Phase 4: Deterministic fix application (no AI)
# ---------------------------------------------------------------------------

@step
async def apply_fixes(raw_csv: str, proposals: list[FixProposal]) -> str:
    """Apply approved fixes deterministically. No AI judgment here."""
    await print(f"[fix] applying {len(proposals)} fixes")
    reader = csv.DictReader(io.StringIO(raw_csv))
    columns = list(reader.fieldnames or [])
    rows = list(reader)
    original_count = len(rows)

    for proposal in proposals:
        if proposal.action == "drop" and "duplicate" in proposal.category.lower():
            seen = set()
            deduped = []
            for r in rows:
                key = tuple(sorted(r.items()))
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            dropped = len(rows) - len(deduped)
            rows = deduped
            await print(f"  [fix] dropped {dropped} duplicate rows")

        elif proposal.action == "drop" and "empty" in proposal.category.lower():
            for col in list(columns):
                if all(not r.get(col, "").strip() for r in rows):
                    columns.remove(col)
                    for r in rows:
                        r.pop(col, None)
                    await print(f"  [fix] dropped empty column: {col}")

        elif proposal.action == "flag":
            flag_col = f"_quality_{proposal.category.lower().replace(' ', '_')}"
            columns.append(flag_col)
            for r in rows:
                r[flag_col] = "flagged"
            await print(f"  [fix] added quality flag column: {flag_col}")

        else:
            await print(f"  [fix] noted (manual): {proposal.category} — {proposal.action}")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    await print(f"[fix] {original_count} → {len(rows)} rows, {len(columns)} columns")
    return output.getvalue()


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

@workflow
async def data_quality(source: str = "data/raw.csv", output: str = "data/clean.csv"):
    timestamp = det.now()
    await print(f"[pipeline] data quality pipeline started at {timestamp}")
    await print(f"[pipeline] input: {source}")

    # Phase 1 — read and validate
    await print("\n[pipeline] === phase 1: reading and validating ===")
    raw_csv = await read_text(source)
    schema = await validate_schema(raw_csv)
    if not schema.valid:
        await print(f"[pipeline] SCHEMA ERRORS: {schema.errors}")
        await input("Schema has errors. Press enter to continue anyway, or Ctrl+C to abort.")

    # Phase 2 — quality checks (deterministic)
    await print("\n[pipeline] === phase 2: quality checks ===")
    quality = await run_quality_checks(raw_csv, schema)

    # Phase 3 — AI diagnosis
    await print("\n[pipeline] === phase 3: AI diagnosis ===")
    proposals = await diagnose_issues(schema, quality)
    for p in proposals:
        await print(f"  [{p.action}] {p.category}: {p.description[:80]}")

    # Phase 4 — human approval
    await print(f"\n{'='*60}")
    await print(f"DATA QUALITY REPORT")
    await print(f"{'='*60}")
    await print(f"Rows: {schema.row_count} | Columns: {len(schema.columns)}")
    await print(f"Issues: {quality.summary}")
    await print(f"\nProposed fixes:")
    for i, p in enumerate(proposals, 1):
        await print(f"  {i}. [{p.action}] {p.category} ({p.affected_rows} rows): {p.description}")

    await input("\nCheckpoint — review proposed fixes. Press enter to apply, or Ctrl+C to abort.")

    # Phase 5 — apply fixes (deterministic)
    await print("\n[pipeline] === phase 5: applying fixes ===")
    clean_csv = await apply_fixes(raw_csv, proposals)

    # Phase 6 — write clean data
    await write_text(output, clean_csv)
    await print(f"\n[pipeline] clean data saved to {output}")

    return {
        "input_rows": schema.row_count,
        "output_file": output,
        "fixes_applied": len(proposals),
    }

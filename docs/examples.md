# Examples

All examples live in [`py-library/examples/`](../examples/).

## Hello agent

See [Getting Started](getting-started.md) for the minimal 5-line workflow.

## PR review — [`pr_review.py`](../examples/pr_review.py)

The canonical end-to-end example. Orchestrates two Claude agents — an **engineer** and
a **reviewer** — to:

1. Implement a small feature on a new branch and push it.
2. Write tests.
3. Run quality gates (`@retry(3)`).
4. Open a draft PR via `gh pr create`.
5. Request a Copilot review and poll for comments.
6. Categorize feedback (Valid / OutOfScope / Invalid / Unclear) and implement fixes.
7. Loop until there are no more comments.
8. On exit (even on failure), close the draft PR in a `finally` block.

Key ideas demonstrated:

- **Agents as values** — `engineer` and `reviewer` are separate `claude_code(...)`
  instances with different configs.
- **Pydantic schemas for structured output** — `QualityReport`, `PRInfo`, `Feedback`.
- **Steps as retry boundaries** — `quality_gates` is `@step @retry(3)`.
- **Human-in-the-loop** — `await input(...)` pauses when feedback is Unclear.
- **Cleanup via `finally`** — the draft PR is always closed, even if the workflow
  crashes.

Run it:

```bash
cd py-library
godel run examples/pr_review.py
```

Prerequisites: `gh auth login` with push access, and the `claude` CLI installed.

## What to read next

- Open `examples/pr_review.py` side-by-side with [concepts.md](concepts.md) and trace
  how each decorator maps to an event in `runs/<run_id>.jsonl`.
- After a run, try:
  ```bash
  godel show <run_id> --graph
  godel tail <run_id> --format json | jq .
  ```

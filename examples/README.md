# Godel Examples

## PR Review Workflow

The canonical M0 example — a real workflow that orchestrates AI agents to implement
a feature, open a draft PR, get it reviewed, and handle feedback.

### Prerequisites

- Python 3.10+
- `pip install -e .` (from `py-library/` directory)
- `claude` CLI installed and authenticated (claude.ai subscription OR ANTHROPIC_API_KEY)
- `gh auth login` completed with push access to the target repository

### Running

```bash
cd py-library
python -m godel run examples/pr_review.py
```

The workflow opens a draft PR on the repository and closes it on exit (whether
by success or exception), so repeated development runs do not accumulate open PRs.

### What it does

1. Engineer agent implements a feature and writes tests
2. Runs quality gates (tests, lint) with up to 3 retries
3. Opens a draft PR
4. Reviewer agent requests and polls for code review
5. Engineer handles feedback (fix, TODO, won't-fix, or escalate)
6. Loop until no more review comments
7. Closes the draft PR on exit (finally block)

# Code Review Guidelines

When reviewing a PR in this project, follow these guidelines. Post findings as inline PR comments using `mcp__github_inline_comment__create_inline_comment` (with `confirmed: true`). Use `gh pr comment` for a top-level summary.

## What to review

### Critical — always flag

- **Security**: command injection, path traversal, unsafe deserialization, hardcoded secrets, SQL injection
- **Determinism violations**: direct use of `random`, `time`, `datetime`, `os.urandom`, or `subprocess` in workflow/step code (must go through `godel.det.*` or `godel._run.run()`)
- **Event log correctness**: changes that could break replay, rewind, or resume (e.g. altering step identity, mutating event shape, skipping event recording)
- **Subprocess bypass**: calling `subprocess.*` directly instead of using `godel._run.run()` — the single audited escape hatch

### High — flag unless clearly intentional

- **Breaking API changes**: modifications to public decorators (`@workflow`, `@step`, `parallel`, `retry`), agent call signatures, or CLI commands without migration path
- **Test coverage**: new features or bug fixes without corresponding tests
- **Silent failures**: bare `except:` or `except Exception: pass` that swallow errors without logging

### Medium — flag with suggestion

- **Code duplication**: logic that already exists elsewhere in the codebase
- **Performance**: O(n^2) or worse in hot paths, unbounded memory growth
- **Error messages**: exceptions that don't provide enough context to diagnose the problem

## What NOT to flag

- Style or formatting (ruff handles this in CI)
- Line length
- Import ordering
- Naming preferences (unless genuinely misleading)
- Missing type hints (not enforced in this project)
- Comments or docstrings (the project prefers minimal comments)

## Output format

Post each finding as a **separate inline comment** on the relevant line using `mcp__github_inline_comment__create_inline_comment` (with `confirmed: true`). Each inline comment should:

1. Start with the severity in bold: **Critical**, **High**, or **Medium**
2. Explain the issue concisely (1-2 sentences)
3. When possible, include a fix using GitHub's suggestion syntax:
   ````
   ```suggestion
   corrected code here
   ```
   ````

After all inline comments are posted, post **one top-level summary** via `gh pr comment` with:
- Total finding count by severity
- A one-line summary per finding (linking to the file and line)
- Overall verdict: approve, request changes, or LGTM

If nothing worth flagging, post only: "LGTM — no issues found."

# Godel CLI Reference

The `godel` command is the single entry point for running, resuming, linting, and inspecting Godel workflows.

## Commands

### `godel run FILE [-- ARG ...]`

Execute a `@workflow`-decorated function from `FILE`.

```
godel run FILE [OPTIONS] [-- ARG ...]
```

**Options:**
- `--no-strict` — Disable strict mode (allow non-deterministic ops).
- `--no-lint` — Skip lint pre-flight check.

**Passing arguments to workflows:**

Append `--` followed by tokens to pass positional and keyword arguments to the `@workflow` function:

```bash
# Positional args only
godel run workflow.py -- alice bob

# Keyword args only
godel run workflow.py -- model=opus max_steps=10

# Mixed (positional order is preserved among positional tokens)
godel run workflow.py -- alice model=opus

# Edge cases
godel run workflow.py -- q=a=b   # key='q', value='a=b'  (split on first '=')
godel run workflow.py -- x=      # key='x', value=''
godel run workflow.py -- 1=foo   # '1' is not a valid identifier → positional
```

**Semantics:**
- Tokens containing `=` with a valid Python identifier LHS become keyword args.
- Other tokens (including `KEY=` where KEY is not a valid identifier) become positional args.
- All values are passed as **strings**; the workflow function is responsible for type coercion.
- Duplicate kwarg keys are rejected with an error.
- Argument binding is validated before the run starts; arity mismatches exit with code 2 and no run ID is printed.

**Workflow function example:**

```python
from godel import workflow

@workflow
async def my_workflow(name: str, model: str = "sonnet"):
    ...
```

```bash
godel run my_workflow.py -- alice model=opus
```

**Exit codes:**
- `0` — Workflow completed or paused successfully.
- `1` — `WorkflowFail` raised inside the workflow.
- `2` — Argument error, no `@workflow` found, or unexpected exception.

---

### `godel resume RUN_ID [FILE]`

Resume a paused or interrupted workflow run from its audit log.

```
godel resume RUN_ID [FILE] [OPTIONS]
```

**Options:**
- `--on-mismatch continue|invalidate|abort` — Policy for `request_hash` mismatches.
- `--on-source-edit warn|abort|ignore` — Policy when a cached `@step`'s source has changed.
- `--no-strict` — Disable strict mode.
- `--no-lint` — Skip lint pre-flight check.

`RUN_ID` can be a prefix (minimum 8 characters) of the full run ID.

The workflow is called with the **same positional and keyword args** that were used in the original `godel run` invocation — no need to re-supply them. The args are recovered from the `WORKFLOW_STARTED` event in the audit log.

**Non-serialisable args:** If the original run was started programmatically with non-JSON-serialisable arguments (e.g. custom Python objects), `godel resume` will refuse with:

```
[godel] resume error: This run used non-serialisable args; programmatic resume only.
```

In that case, resume the workflow directly in Python code.

---

### `godel lint FILE`

Lint a workflow file for common mistakes.

```
godel lint FILE [--format text|json] [--skip RULE_IDS]
```

**Options:**
- `--format text|json` — Output format (default: `text`).
- `--skip RULE_IDS` — Comma-separated rule IDs to skip (e.g. `PL003,PL007`).

**Exit codes:** `1` if any errors found; `0` if warnings only or clean.

---

### `godel show RUN_ID`

Display the audit log for a workflow run.

```
godel show RUN_ID [--graph] [--all]
```

**Options:**
- `--graph` — Render the DAG as an ASCII tree.
- `--all` — Show failed retries and invalidated events.

---

### `godel pause RUN_ID`

Request a live workflow run to pause at its next `@step` boundary.

```
godel pause RUN_ID [--reason TEXT]
```

---

### `godel resume RUN_ID`

See [resume](#godel-resume-run_id-file) above.

---

### `godel rewind RUN_ID`

Rewind a workflow run to a previous checkpoint.

```
godel rewind RUN_ID --to EVENT_ID[,EVENT_ID,...] [--reason TEXT]
```

---

### `godel repair RUN_ID`

Drop an intervention agent into a paused or crashed run.

```
godel repair RUN_ID [--agent MODULE:FUNCTION] [--model MODEL] [--max-iterations N] [--dry-run]
```

---

### `godel tail RUN_ID`

Follow a workflow's audit log in real time.

```
godel tail RUN_ID [--format pretty|json] [--no-follow] [--no-wait]
```

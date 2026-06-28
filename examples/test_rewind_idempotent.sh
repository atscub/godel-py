#!/usr/bin/env bash
# End-to-end test: rewind --assume-idempotent
# Runs the demo workflow, then verifies rewind fails without the flag
# and succeeds with it.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Step 1: Run the workflow ==="
RUN_OUTPUT=$(python -m godel run examples/rewind_idempotent_demo.py 2>&1)
RUN_ID=$(echo "$RUN_OUTPUT" | grep -oP '(?<=run )[0-9a-f-]+')
echo "Run ID: $RUN_ID"
echo "$RUN_OUTPUT"
echo

# Find the step.enter event for read_only_check
STEP_EID=$(python3 -c "
import json
events = {}
with open('runs/${RUN_ID}.jsonl') as f:
    for line in f:
        e = json.loads(line)
        events[e['event_id']] = e
for eid, e in events.items():
    if e['op'] == 'step.enter' and 'read_only_check' in '/'.join(e.get('step_path', [])):
        print(eid)
        break
")
echo "=== Step 2: Target event: $STEP_EID (step.enter read_only_check) ==="
echo

echo "=== Step 3: Rewind WITHOUT --assume-idempotent (expect failure) ==="
set +e
REWIND_OUT=$(python -m godel rewind "$RUN_ID" --to "$STEP_EID" 2>&1)
REWIND_EXIT=$?
set -e
echo "$REWIND_OUT"
if [ $REWIND_EXIT -eq 2 ]; then
    echo "PASS: Rewind correctly refused (exit 2)"
else
    echo "FAIL: Expected exit 2, got $REWIND_EXIT"
    exit 1
fi
echo

echo "=== Step 4: Rewind WITH --assume-idempotent (expect success) ==="
REWIND_OUT=$(python -m godel rewind "$RUN_ID" --to "$STEP_EID" --assume-idempotent 2>&1)
REWIND_EXIT=$?
echo "$REWIND_OUT"
if [ $REWIND_EXIT -eq 0 ]; then
    echo "PASS: Rewind succeeded with --assume-idempotent (exit 0)"
else
    echo "FAIL: Expected exit 0, got $REWIND_EXIT"
    exit 1
fi

if echo "$REWIND_OUT" | grep -qi "warning"; then
    echo "PASS: Warning was emitted"
else
    echo "FAIL: No warning emitted"
    exit 1
fi
echo

echo "=== Step 5: Resume (re-executes from rewind point) ==="
RESUME_OUT=$(python -m godel resume "$RUN_ID" 2>&1)
echo "$RESUME_OUT"
if echo "$RESUME_OUT" | grep -q "completed"; then
    echo "PASS: Resume completed successfully"
else
    echo "FAIL: Resume did not complete"
    exit 1
fi
echo

echo "=== All checks passed ==="

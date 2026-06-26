"""Tests for ASCII DAG renderer."""
from godel._dag_render import render_dag_plain, _partition_events
from godel._events import Event, EventStatus


def _ev(eid, op, status=EventStatus.FINISHED, step_path=(), **kwargs):
    return Event(
        event_id=eid, run_id="test", seq=0,
        op=op, status=status, step_path=step_path,
        ts_start="2026-01-01T00:00:00+00:00",
        ts_end="2026-01-01T00:00:01+00:00",
        **kwargs,
    )


def test_empty_run():
    assert render_dag_plain([]) == "(empty run)"


def test_simple_chain():
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED"),
        _ev("B" * 26, "step.enter", step_path=("quality_gates",)),
        _ev("C" * 26, "run", request={"cmd": "npm test"}),
    ]
    output = render_dag_plain(events)
    assert "WORKFLOW_STARTED" in output
    assert "step.enter" in output
    assert "npm test" in output
    assert "\u2713" in output


def test_fork_join():
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED"),
        _ev("F" * 26, "FORK", request={"branches": 2}),
        _ev("B1" * 13, "run", request={"cmd": "echo A"}),
        _ev("B2" * 13, "run", request={"cmd": "echo B"}),
        _ev("J" * 26, "JOIN"),
    ]
    output = render_dag_plain(events)
    assert "FORK" in output
    assert "2 branches" in output
    assert "JOIN" in output


def test_failed_event():
    events = [
        _ev("A" * 26, "step.enter", status=EventStatus.FAILED, step_path=("bad",)),
    ]
    output = render_dag_plain(events)
    assert "\u2717" in output


def test_started_event():
    events = [
        _ev("A" * 26, "step.enter", status=EventStatus.STARTED, step_path=("running",)),
    ]
    output = render_dag_plain(events)
    assert "\u25cc" in output


def test_fork_branch_separation():
    """Branch headers appear when step_path changes inside a FORK."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED"),
        _ev("F" * 26, "FORK", request={"branches": 2}),
        _ev("B1" * 13, "det.now", step_path=("branch_a",)),
        _ev("B2" * 13, "det.now", step_path=("branch_a",)),
        _ev("C1" * 13, "det.now", step_path=("branch_b",)),
        _ev("J" * 26, "JOIN"),
    ]
    output = render_dag_plain(events)
    lines = output.splitlines()

    # Should contain branch header lines
    branch_a_headers = [ln for ln in lines if "branch: branch_a" in ln]
    branch_b_headers = [ln for ln in lines if "branch: branch_b" in ln]
    assert len(branch_a_headers) == 1, f"Expected 1 branch_a header, got {branch_a_headers}"
    assert len(branch_b_headers) == 1, f"Expected 1 branch_b header, got {branch_b_headers}"

    # Branch headers should use the ┌─ connector
    assert "\u250c\u2500 branch: branch_a" in output
    assert "\u250c\u2500 branch: branch_b" in output

    # branch_a header should come before branch_b header
    a_idx = output.index("branch: branch_a")
    b_idx = output.index("branch: branch_b")
    assert a_idx < b_idx

    # Only one header per branch even with multiple events in same branch
    assert output.count("branch: branch_a") == 1

    # Branch content should be indented one level deeper than branch header
    for line in lines:
        if "branch: branch_a" in line:
            header_prefix = line.split("\u250c")[0]  # prefix before ┌
        if "det.now" in line and "branch_a" in line:
            event_prefix = line.split("\u251c")[0]  # prefix before ├
            # Event prefix should be one │-level deeper than header prefix
            assert len(event_prefix) > len(header_prefix), (
                f"Branch content should be indented deeper than header.\n"
                f"Header prefix: {header_prefix!r}\nEvent prefix: {event_prefix!r}"
            )


def test_branch_content_indented_under_header():
    """Branch events are indented one level deeper than the branch header."""
    events = [
        _ev("a" * 26, "FORK", request={"branches": 2}),
        _ev("b" * 26, "det.now", step_path=("branch_a",)),
        _ev("c" * 26, "det.now", step_path=("branch_b",)),
        _ev("d" * 26, "JOIN"),
    ]
    output = render_dag_plain(events)
    lines = output.splitlines()

    # FORK line at indent 0: ├─ [...]
    assert lines[0].startswith("\u251c\u2500")

    # branch_a header at indent 1: │  ┌─ branch: branch_a
    assert lines[1] == "\u2502  \u250c\u2500 branch: branch_a"

    # branch_a event at indent 2: │  │  ├─ [...]
    assert lines[2].startswith("\u2502  \u2502  \u251c\u2500")
    assert "det.now" in lines[2]
    assert "branch_a" in lines[2]

    # branch_b header at indent 1: │  ┌─ branch: branch_b
    assert lines[3] == "\u2502  \u250c\u2500 branch: branch_b"

    # branch_b event at indent 2: │  │  ├─ [...]
    assert lines[4].startswith("\u2502  \u2502  \u251c\u2500")
    assert "det.now" in lines[4]
    assert "branch_b" in lines[4]

    # JOIN at indent 0: ├─ [...] JOIN
    assert lines[5].startswith("\u251c\u2500")
    assert "JOIN" in lines[5]


# --- Filtering and grouping tests ---


def test_default_hides_retried_failures():
    """Default view hides FAILED events when same step later succeeded."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED", status=EventStatus.FAILED),
        _ev("B" * 26, "step.enter", step_path=("work",), status=EventStatus.FAILED),
        _ev("C" * 26, "step.enter", step_path=("work",), status=EventStatus.FINISHED),
        _ev("D" * 26, "WORKFLOW_STARTED", status=EventStatus.FINISHED),
    ]
    output = render_dag_plain(events)
    # Should contain only the successful events
    assert "\u2717" not in output  # no ✗
    assert "\u2713" in output      # has ✓
    # Only one WORKFLOW_STARTED
    assert output.count("WORKFLOW_STARTED") == 1


def test_default_hides_invalidated():
    """Default view hides INVALIDATED events."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED"),
        _ev("B" * 26, "step.enter", step_path=("work",)),
        _ev("C" * 26, "step.enter", step_path=("old",), status=EventStatus.INVALIDATED),
    ]
    output = render_dag_plain(events)
    assert "old" not in output
    assert "\u2298" not in output  # no ⊘


def test_default_keeps_unsuperseded_failures():
    """Default view keeps FAILED events that were never retried successfully."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED", status=EventStatus.FAILED),
        _ev("B" * 26, "step.enter", step_path=("work",), status=EventStatus.FAILED),
    ]
    output = render_dag_plain(events)
    assert "\u2717" in output  # ✗ present — these failures are the final state
    assert output.count("WORKFLOW_STARTED") == 1
    assert "step.enter" in output


def test_show_all_groups_retries():
    """--all view groups prior failures before the successful event."""
    events = [
        _ev("A" * 26, "step.enter", step_path=("work",), status=EventStatus.FAILED),
        _ev("B" * 26, "step.enter", step_path=("work",), status=EventStatus.FINISHED),
    ]
    output = render_dag_plain(events, show_all=True)
    assert "prior attempt" in output
    assert "succeeded" in output
    assert "\u2717" in output  # ✗ in the retry group
    assert "\u2713" in output  # ✓ for the success


def test_show_all_shows_invalidated_section():
    """--all view shows invalidated events in a grouped section."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED"),
        _ev("B" * 26, "step.enter", step_path=("work",)),
        _ev("C" * 26, "step.enter", step_path=("old",), status=EventStatus.INVALIDATED),
    ]
    output = render_dag_plain(events, show_all=True)
    assert "invalidated (rewind)" in output
    assert "\u2298" in output  # ⊘ icon
    assert "old" in output


def test_partition_deduplicates_workflow_started():
    """_partition_events keeps only the last WORKFLOW_STARTED, discards extras silently."""
    events = [
        _ev("A" * 26, "WORKFLOW_STARTED", status=EventStatus.FAILED),
        _ev("B" * 26, "step.enter", step_path=("s",)),
        _ev("C" * 26, "WORKFLOW_STARTED", status=EventStatus.FINISHED),
    ]
    effective, retries, invalidated = _partition_events(events)
    wf = [e for e in effective if e.op == "WORKFLOW_STARTED"]
    assert len(wf) == 1
    assert wf[0].status == EventStatus.FINISHED
    # Failed WORKFLOW_STARTED is silently discarded, NOT in retries
    assert not retries

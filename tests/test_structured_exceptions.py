"""Tests for the structured exception hierarchy (M5)."""
from __future__ import annotations


from godel._exceptions import (
    GodelError,
    AgentRefusal,
    SchemaValidationFailure,
    HumanTimeout,
    NonDeterministicEscape,
    RewindUnsafe,
)
from godel._run import CommandFailure
from godel._decorators import WorkflowFail


# ---------------------------------------------------------------------------
# GodelError base
# ---------------------------------------------------------------------------

class TestGodelError:
    def test_instantiate_no_args(self):
        exc = GodelError()
        assert isinstance(exc, Exception)

    def test_instantiate_all_kwargs(self):
        exc = GodelError(
            "something went wrong",
            step_path=("main", "validate"),
            source_location="workflow.gdl:42",
            remediation_hint="check the schema",
        )
        assert exc.step_path == ("main", "validate")
        assert exc.source_location == "workflow.gdl:42"
        assert exc.remediation_hint == "check the schema"

    def test_str_contains_context_marker(self):
        exc = GodelError(
            "base error",
            step_path=("root", "child"),
            source_location="foo.gdl:10",
            remediation_hint="retry with idempotent=True",
        )
        s = str(exc)
        assert "[godel:" in s
        assert "step=root/child" in s
        assert "source=foo.gdl:10" in s
        assert "hint=retry with idempotent=True" in s

    def test_context_marker_empty_when_no_structured_fields(self):
        exc = GodelError("just a message")
        assert exc._context_marker() == ""
        assert str(exc) == "just a message"

    def test_context_marker_partial_fields(self):
        exc = GodelError("partial", step_path=("step1",))
        marker = exc._context_marker()
        assert "step=step1" in marker
        assert "source=" not in marker
        assert "hint=" not in marker

    def test_str_no_leading_space_when_message_is_empty(self):
        # Regression: GodelError('', step_path=...) must not produce ' [godel:...]'
        exc = GodelError("", step_path=("a",), source_location="f.gdl:1")
        s = str(exc)
        assert not s.startswith(" "), f"leading space in str: {s!r}"
        assert s == "[godel:step=a, source=f.gdl:1]"

    def test_context_marker_exact_format(self):
        # Lock the exact format so parsers don't silently break on whitespace changes.
        exc = GodelError(
            "msg",
            step_path=("main", "validate"),
            source_location="foo.gdl:10",
            remediation_hint="retry",
        )
        assert exc._context_marker() == "[godel:step=main/validate, source=foo.gdl:10, hint=retry]"

    def test_context_marker_filters_empty_string_path_components(self):
        # WARN-1: step_path=('',) must not produce 'step=' or 'step=/...'
        exc = GodelError("msg", step_path=("",))
        assert exc._context_marker() == ""

    def test_context_marker_filters_interior_empty_path_component(self):
        # WARN-1: interior empty strings must be stripped → no double-slash
        exc = GodelError("msg", step_path=("a", "", "b"))
        marker = exc._context_marker()
        assert "a/b" in marker
        assert "//" not in marker

    def test_context_marker_filters_whitespace_only_path_component(self):
        # WARN-1: whitespace-only strings must also be stripped (not just empty)
        exc = GodelError("msg", step_path=("   ",))
        assert exc._context_marker() == ""

    def test_context_marker_filters_mixed_whitespace_path_components(self):
        # WARN-1: whitespace-only components mixed with valid ones must be dropped
        exc = GodelError("msg", step_path=("a", "   ", "b"))
        marker = exc._context_marker()
        assert "a/b" in marker
        assert "   " not in marker
        assert "//" not in marker

    def test_context_marker_filters_none_in_path(self):
        # None values in step_path must be silently filtered, not crash with AttributeError
        from godel._exceptions import _render_context_marker
        result = _render_context_marker((None, "valid", None), "", "")  # type: ignore[arg-type]
        assert "step=valid" in result
        assert "None" not in result


# ---------------------------------------------------------------------------
# AgentRefusal
# ---------------------------------------------------------------------------

class TestAgentRefusal:
    def test_instantiate_with_all_fields(self):
        exc = AgentRefusal(
            "model refused",
            model="claude-opus-4",
            refusal_reason="content policy",
            step_path=("generate",),
            source_location="wf.gdl:5",
            remediation_hint="rephrase the prompt",
        )
        assert exc.model == "claude-opus-4"
        assert exc.refusal_reason == "content policy"
        assert exc.step_path == ("generate",)

    def test_is_godel_error(self):
        assert issubclass(AgentRefusal, GodelError)

    def test_str_contains_marker(self):
        exc = AgentRefusal(
            "refused",
            model="gpt-5",
            step_path=("gen",),
            source_location="a.gdl:1",
            remediation_hint="try again",
        )
        assert "[godel:" in str(exc)

    def test_domain_fields_accessible(self):
        exc = AgentRefusal("r", model="m", refusal_reason="rr")
        assert exc.model == "m"
        assert exc.refusal_reason == "rr"


# ---------------------------------------------------------------------------
# SchemaValidationFailure
# ---------------------------------------------------------------------------

class TestSchemaValidationFailure:
    def test_instantiate_with_all_fields(self):
        exc = SchemaValidationFailure(
            "schema mismatch",
            schema_name="OutputSchema",
            validation_errors=["field 'id' missing", "field 'name' wrong type"],
            step_path=("validate",),
            source_location="wf.gdl:20",
            remediation_hint="fix the output schema",
        )
        assert exc.schema_name == "OutputSchema"
        assert exc.validation_errors == ["field 'id' missing", "field 'name' wrong type"]

    def test_is_godel_error(self):
        assert issubclass(SchemaValidationFailure, GodelError)

    def test_validation_errors_default_empty_list(self):
        exc = SchemaValidationFailure("err")
        assert exc.validation_errors == []

    def test_str_contains_marker(self):
        exc = SchemaValidationFailure(
            "fail",
            step_path=("s",),
            source_location="f.gdl:1",
            remediation_hint="h",
        )
        assert "[godel:" in str(exc)


# ---------------------------------------------------------------------------
# HumanTimeout
# ---------------------------------------------------------------------------

class TestHumanTimeout:
    def test_instantiate_with_all_fields(self):
        exc = HumanTimeout(
            "timed out waiting for human",
            prompt="Please approve the plan",
            timeout_seconds=300.0,
            step_path=("await_approval",),
            source_location="wf.gdl:55",
            remediation_hint="increase timeout or set auto-approve",
        )
        assert exc.prompt == "Please approve the plan"
        assert exc.timeout_seconds == 300.0

    def test_is_godel_error(self):
        assert issubclass(HumanTimeout, GodelError)

    def test_str_contains_marker(self):
        exc = HumanTimeout(
            "timeout",
            step_path=("s",),
            source_location="f.gdl:1",
            remediation_hint="h",
        )
        assert "[godel:" in str(exc)

    def test_domain_fields_accessible(self):
        exc = HumanTimeout("t", prompt="approve?", timeout_seconds=60.0)
        assert exc.prompt == "approve?"
        assert exc.timeout_seconds == 60.0

    def test_timeout_seconds_defaults_to_none(self):
        # NIT: default should be None (unknown), not 0.0 (a real zero-second timeout)
        exc = HumanTimeout("timed out")
        assert exc.timeout_seconds is None


# ---------------------------------------------------------------------------
# NonDeterministicEscape
# ---------------------------------------------------------------------------

class TestNonDeterministicEscape:
    def test_instantiate_with_all_fields(self):
        exc = NonDeterministicEscape(
            "random() is not allowed",
            operation="random.randint",
            step_path=("compute",),
            source_location="wf.gdl:88",
            remediation_hint="use det.randint instead",
        )
        assert exc.operation == "random.randint"

    def test_is_godel_error(self):
        assert issubclass(NonDeterministicEscape, GodelError)

    def test_str_contains_marker(self):
        exc = NonDeterministicEscape(
            "escape",
            step_path=("s",),
            source_location="f.gdl:1",
            remediation_hint="h",
        )
        assert "[godel:" in str(exc)


# ---------------------------------------------------------------------------
# RewindUnsafe
# ---------------------------------------------------------------------------

class TestRewindUnsafe:
    def test_instantiate_with_all_fields(self):
        exc = RewindUnsafe(
            "cannot rewind past non-idempotent op",
            event_id="evt-abc-123",
            op="run",
            cmd="rm -rf /tmp/build",
            step_path=("build",),
            source_location="wf.gdl:77",
            remediation_hint="mark as idempotent or skip",
        )
        assert exc.event_id == "evt-abc-123"
        assert exc.op == "run"
        assert exc.cmd == "rm -rf /tmp/build"

    def test_cmd_can_be_none(self):
        exc = RewindUnsafe("unsafe", event_id="e1", op="notify", cmd=None)
        assert exc.cmd is None

    def test_is_godel_error(self):
        assert issubclass(RewindUnsafe, GodelError)

    def test_str_contains_marker(self):
        exc = RewindUnsafe(
            "unsafe",
            step_path=("s",),
            source_location="f.gdl:1",
            remediation_hint="h",
        )
        assert "[godel:" in str(exc)


# ---------------------------------------------------------------------------
# CommandFailure — structured fields and inheritance
# ---------------------------------------------------------------------------

class TestCommandFailure:
    def test_still_inherits_from_workflow_fail(self):
        assert issubclass(CommandFailure, WorkflowFail)

    def test_instantiate_with_structured_fields(self):
        exc = CommandFailure(
            "command failed",
            stdout="some output",
            stderr="error output",
            returncode=1,
            step_path=("build", "compile"),
            source_location="wf.gdl:30",
            remediation_hint="check the build logs",
        )
        assert exc.stdout == "some output"
        assert exc.stderr == "error output"
        assert exc.returncode == 1
        assert exc.step_path == ("build", "compile")
        assert exc.source_location == "wf.gdl:30"
        assert exc.remediation_hint == "check the build logs"

    def test_str_contains_context_marker(self):
        exc = CommandFailure(
            "cmd failed",
            step_path=("build",),
            source_location="wf.gdl:10",
            remediation_hint="retry",
        )
        s = str(exc)
        assert "[godel:" in s
        assert "step=build" in s
        assert "source=wf.gdl:10" in s
        assert "hint=retry" in s

    def test_context_marker_empty_without_structured_fields(self):
        exc = CommandFailure("simple failure", stdout="out", stderr="err", returncode=2)
        assert exc._context_marker() == ""
        assert str(exc) == "simple failure"

    def test_backward_compat_minimal_init(self):
        exc = CommandFailure("failed")
        assert exc.stdout == ""
        assert exc.stderr == ""
        assert exc.returncode is None
        assert exc.step_path == ()

    def test_str_no_leading_space_when_message_is_empty(self):
        # Regression: CommandFailure('', step_path=...) must not produce ' [godel:...]'
        exc = CommandFailure("", step_path=("build",))
        s = str(exc)
        assert not s.startswith(" "), f"leading space in str: {s!r}"
        assert s == "[godel:step=build]"

    def test_context_marker_filters_empty_string_path_components(self):
        # WARN-1 + WARN-2: CommandFailure delegates to shared helper, so same
        # filtering must apply.
        exc = CommandFailure("failed", step_path=("",))
        assert exc._context_marker() == ""

    def test_context_marker_uses_shared_helper(self):
        # WARN-2: Confirm CommandFailure produces the same marker as GodelError
        # would for the same inputs — ensures no format divergence.
        from godel._exceptions import _render_context_marker
        exc = CommandFailure(
            "fail",
            step_path=("build", "compile"),
            source_location="wf.gdl:30",
            remediation_hint="check logs",
        )
        expected = _render_context_marker(("build", "compile"), "wf.gdl:30", "check logs")
        assert exc._context_marker() == expected

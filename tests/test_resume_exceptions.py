"""Tests for ResumeError and UnsafeResumeError exception classes."""
from __future__ import annotations

import pytest

from godel._exceptions import GodelError, ResumeError, UnsafeResumeError


class TestResumeError:
    def test_is_exception(self):
        err = ResumeError("something broke")
        assert isinstance(err, Exception)

    def test_message_preserved(self):
        err = ResumeError("corrupted log")
        assert str(err) == "corrupted log"
        assert err.args == ("corrupted log",)


class TestUnsafeResumeError:
    def test_inherits_resume_error(self):
        err = UnsafeResumeError("unsafe op")
        assert isinstance(err, ResumeError)
        assert isinstance(err, Exception)

    def test_attributes_set(self):
        err = UnsafeResumeError(
            "partial execution",
            event_id="evt-123",
            cmd="git push",
            step_path=("workflow", "deploy", "push"),
        )
        assert err.event_id == "evt-123"
        assert err.cmd == "git push"
        assert err.step_path == ("workflow", "deploy", "push")

    def test_default_attributes(self):
        err = UnsafeResumeError("msg")
        assert err.event_id == ""
        assert err.cmd == ""
        assert err.step_path == ()

    def test_message_in_args(self):
        err = UnsafeResumeError("the message")
        assert err.args == ("the message",)

    def test_str_includes_fix_suggestion(self):
        err = UnsafeResumeError("dangerous")
        text = str(err)
        assert "idempotent=True" in text
        assert "godel rewind" in text

    def test_str_includes_command(self):
        err = UnsafeResumeError("fail", cmd="rm -rf /")
        text = str(err)
        assert "Command: rm -rf /" in text

    def test_str_includes_step_path(self):
        err = UnsafeResumeError("fail", step_path=("build", "compile"))
        text = str(err)
        assert "Step: build/compile" in text

    def test_str_omits_command_when_empty(self):
        err = UnsafeResumeError("fail")
        text = str(err)
        assert "Command:" not in text

    def test_str_omits_step_when_empty(self):
        err = UnsafeResumeError("fail")
        text = str(err)
        assert "Step:" not in text

    def test_str_starts_with_class_name(self):
        err = UnsafeResumeError("oops")
        assert str(err).startswith("UnsafeResumeError:")

    def test_str_full_format(self):
        err = UnsafeResumeError(
            "non-idempotent run",
            event_id="e1",
            cmd="deploy prod",
            step_path=("ci", "deploy"),
        )
        text = str(err)
        lines = text.split("\n")
        assert lines[0] == "UnsafeResumeError: non-idempotent run"
        assert "  Command: deploy prod" in lines
        assert "  Step: ci/deploy" in lines
        assert any("idempotent=True" in l for l in lines)
        assert any("godel rewind" in l for l in lines)

    def test_catchable_as_resume_error(self):
        with pytest.raises(ResumeError):
            raise UnsafeResumeError("boom")

    def test_catchable_as_exception(self):
        with pytest.raises(Exception):
            raise UnsafeResumeError("boom")

    def test_catchable_as_godel_error(self):
        with pytest.raises(GodelError):
            raise UnsafeResumeError("boom")


class TestResumeErrorGodelErrorHierarchy:
    """ResumeError is a GodelError subclass — verify catch-all compatibility."""

    def test_resume_error_is_godel_error(self):
        err = ResumeError("bad log")
        assert isinstance(err, GodelError)

    def test_resume_error_catchable_as_godel_error(self):
        with pytest.raises(GodelError):
            raise ResumeError("corrupted")

    def test_unsafe_resume_error_is_godel_error(self):
        err = UnsafeResumeError("unsafe")
        assert isinstance(err, GodelError)

    def test_resume_error_str_unchanged(self):
        """GodelError base __str__ must not add noise when context fields are unset."""
        err = ResumeError("corrupted log")
        assert str(err) == "corrupted log"

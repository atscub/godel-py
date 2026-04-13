"""Tests for godel exception hierarchy."""
from godel._exceptions import GodelStrictError, StrictViolation


def test_strict_violation_fields():
    v = StrictViolation(file="test.py", line=10, col=5, message="banned call", layer="ast")
    assert v.file == "test.py"
    assert v.line == 10
    assert v.col == 5
    assert v.layer == "ast"


def test_godel_strict_error_single_violation():
    v = StrictViolation(file="test.py", line=10, col=5, message="banned call: time.time()", layer="ast")
    err = GodelStrictError([v])
    assert len(err.violations) == 1
    s = str(err)
    assert "1 violation" in s
    assert "test.py:10:5" in s
    assert "banned call: time.time()" in s
    assert "[ast]" in s


def test_godel_strict_error_multiple_violations():
    vs = [
        StrictViolation(file="a.py", line=1, col=0, message="msg1", layer="ast"),
        StrictViolation(file="b.py", line=2, col=3, message="msg2", layer="import"),
    ]
    err = GodelStrictError(vs)
    s = str(err)
    assert "2 violation" in s
    assert "a.py:1:0" in s
    assert "b.py:2:3" in s


def test_godel_strict_error_zero_line():
    v = StrictViolation(file="<runtime>", line=0, col=0, message="runtime block", layer="audit")
    err = GodelStrictError([v])
    s = str(err)
    assert "<runtime>" in s
    assert ":" not in s.split("<runtime>")[1].split("—")[0].strip() or "0:0" not in s


def test_godel_strict_error_is_exception():
    v = StrictViolation(file="x.py", line=1, col=0, message="test", layer="ast")
    err = GodelStrictError([v])
    assert isinstance(err, Exception)


def test_godel_strict_error_custom_message():
    v = StrictViolation(file="x.py", line=1, col=0, message="test", layer="ast")
    err = GodelStrictError([v], message="custom error")
    assert str(err.args[0]) == "custom error"

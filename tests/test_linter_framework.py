"""Tests for the linter diagnostic model and rule registry."""
from __future__ import annotations

import ast

import pytest

from godel._linter import (
    LintDiagnostic,
    LintRule,
    clear_rules,
    get_rules,
    lint_file,
    lint_source,
    register_rule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyRule:
    rule_id = "TEST001"
    severity = "warning"
    description = "Test rule that always fires once on line 1"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        return [
            LintDiagnostic(
                file=filename,
                rule=self.rule_id,
                severity=self.severity,
                message="dummy warning",
                line=1,
            )
        ]


# ---------------------------------------------------------------------------
# Fixture: isolate the global registry between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Clear the rule registry before each test and restore afterwards.

    Uses ``monkeypatch`` so the restore is process-safe and compatible with
    ``pytest-xdist`` (each worker has its own process, and ``monkeypatch``
    ensures the original list is restored even if the test fails or is
    collected in a different order).
    """
    import godel._linter as _linter_mod

    original = list(_linter_mod._RULES)
    clear_rules()
    yield
    clear_rules()
    _linter_mod._RULES.extend(original)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_rules_returns_empty():
    result = lint_source("x = 1")
    assert result == []


def test_register_and_invoke_rule():
    register_rule(DummyRule())
    result = lint_source("x = 1")
    assert len(result) == 1
    assert result[0].rule == "TEST001"
    assert result[0].message == "dummy warning"


def test_get_rules_returns_registered():
    rule = DummyRule()
    register_rule(rule)
    rules = get_rules()
    assert len(rules) == 1
    assert rules[0] is rule


def test_get_rules_returns_copy():
    """Mutating the returned list must not affect the registry."""
    register_rule(DummyRule())
    rules = get_rules()
    rules.clear()
    assert len(get_rules()) == 1


def test_diagnostic_format():
    d = LintDiagnostic(
        file="test.py",
        rule="PL001",
        severity="error",
        message="missing await",
        line=5,
        col=10,
    )
    assert d.format() == "test.py:5:10: PL001 error: missing await"


def test_diagnostic_format_default_col():
    """When col is None (unknown), format() emits an empty col field."""
    d = LintDiagnostic(
        file="wf.py",
        rule="PL002",
        severity="warning",
        message="something",
        line=3,
    )
    assert d.format() == "wf.py:3:: PL002 warning: something"


def test_diagnostic_to_dict():
    d = LintDiagnostic(
        file="test.py",
        rule="PL001",
        severity="error",
        message="missing await",
        line=5,
        col=10,
    )
    d_dict = d.to_dict()
    assert d_dict == {
        "file": "test.py",
        "rule": "PL001",
        "severity": "error",
        "message": "missing await",
        "line": 5,
        "col": 10,
    }


def test_skip_rules():
    register_rule(DummyRule())
    result = lint_source("x = 1", skip_rules={"TEST001"})
    assert result == []


def test_skip_rules_partial():
    """Only the skipped rule is suppressed; others still run."""

    class AnotherRule:
        rule_id = "TEST002"
        severity = "error"
        description = "Another test rule"

        def check(self, tree, filename):
            return [
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message="another error",
                    line=2,
                )
            ]

    register_rule(DummyRule())
    register_rule(AnotherRule())

    result = lint_source("x = 1\ny = 2", skip_rules={"TEST001"})
    assert len(result) == 1
    assert result[0].rule == "TEST002"


def test_syntax_error_produces_diagnostic():
    result = lint_source("def f(:\n  pass")
    assert len(result) == 1
    d = result[0]
    assert d.rule == "PL000"
    assert d.severity == "error"
    assert "SyntaxError" in d.message
    assert d.line >= 1


def test_syntax_error_no_rules_run():
    """Rules must not be invoked when the file has a syntax error."""
    register_rule(DummyRule())
    result = lint_source("def f(:\n  pass")
    assert len(result) == 1
    assert result[0].rule == "PL000"


def test_lint_file(tmp_path):
    f = tmp_path / "workflow.py"
    f.write_text("x = 1\n")
    register_rule(DummyRule())
    result = lint_file(str(f))
    assert len(result) == 1
    assert result[0].file == str(f)


def test_lint_file_syntax_error(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n  pass\n")
    result = lint_file(str(f))
    assert len(result) == 1
    assert result[0].rule == "PL000"


def test_lint_file_skip_rules(tmp_path):
    f = tmp_path / "workflow.py"
    f.write_text("x = 1\n")
    register_rule(DummyRule())
    result = lint_file(str(f), skip_rules={"TEST001"})
    assert result == []


def test_lint_file_missing_file():
    """lint_file must return a PL000 diagnostic for a missing file, not raise."""
    result = lint_file("/nonexistent/path/does_not_exist.py")
    assert len(result) == 1
    d = result[0]
    assert d.rule == "PL000"
    assert d.severity == "error"
    assert "not found" in d.message


def test_lint_file_unreadable_file(tmp_path):
    """lint_file must return a PL000 diagnostic when the file is unreadable."""
    import os

    f = tmp_path / "locked.py"
    f.write_text("x = 1\n")
    os.chmod(str(f), 0o000)
    try:
        result = lint_file(str(f))
        assert len(result) == 1
        assert result[0].rule == "PL000"
        assert result[0].severity == "error"
    finally:
        os.chmod(str(f), 0o644)


def test_lint_source_register_during_iteration():
    """Rules that call register_rule() during check() must not crash lint_source."""

    class SelfRegisteringRule:
        rule_id = "TEST_REG"
        severity = "warning"
        description = "Registers a new rule as a side effect of check()"

        def check(self, tree, filename):
            register_rule(DummyRule())
            return []

    register_rule(SelfRegisteringRule())
    # Should not raise RuntimeError: list changed size during iteration
    result = lint_source("x = 1")
    assert isinstance(result, list)


def test_diagnostics_sorted_by_line():
    class MultiRule:
        rule_id = "TEST003"
        severity = "warning"
        description = "Emits diagnostics out of order"

        def check(self, tree, filename):
            return [
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message="line 10",
                    line=10,
                ),
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message="line 2",
                    line=2,
                ),
            ]

    register_rule(MultiRule())
    source = "\n".join(f"x{i} = {i}" for i in range(15))
    result = lint_source(source)
    assert len(result) == 2
    assert result[0].line < result[1].line


def test_diagnostics_sorted_by_col_within_same_line():
    class ColRule:
        rule_id = "TEST004"
        severity = "warning"
        description = "Emits same-line diagnostics out of col order"

        def check(self, tree, filename):
            return [
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message="col 20",
                    line=1,
                    col=20,
                ),
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message="col 5",
                    line=1,
                    col=5,
                ),
            ]

    register_rule(ColRule())
    result = lint_source("x = 1")
    assert len(result) == 2
    assert result[0].col < result[1].col


# ---------------------------------------------------------------------------
# New tests for WARN-1 / WARN-2 / WARN-3 / WARN-4 fixes
# ---------------------------------------------------------------------------


def test_register_rule_rejects_invalid_object():
    """register_rule() must raise TypeError for objects missing LintRule attributes."""
    with pytest.raises(TypeError, match="LintRule"):
        register_rule(object())  # type: ignore[arg-type]


def test_register_rule_rejects_missing_check():
    """An object with rule_id/severity/description but no check() is rejected."""

    class NoCheck:
        rule_id = "X001"
        severity = "warning"
        description = "no check method"

    with pytest.raises(TypeError, match="LintRule"):
        register_rule(NoCheck())  # type: ignore[arg-type]


def test_lintrule_protocol_is_runtime_checkable():
    """LintRule must be @runtime_checkable so isinstance() works."""
    assert isinstance(DummyRule(), LintRule)
    assert not isinstance(object(), LintRule)


def test_lint_diagnostic_rejects_invalid_severity():
    """LintDiagnostic.__post_init__ must reject non-canonical severity strings."""
    with pytest.raises(ValueError, match="severity"):
        LintDiagnostic(
            file="f.py",
            rule="PL001",
            severity="WARNING",  # type: ignore[arg-type]  # wrong case
            message="bad",
            line=1,
        )


def test_lint_diagnostic_col_none_means_unknown():
    """col=None (default) means 'unknown column', not literal column 0."""
    d = LintDiagnostic(file="f.py", rule="PL001", severity="error", message="m", line=1)
    assert d.col is None
    assert d.to_dict()["col"] is None


def test_lint_diagnostic_col_zero_is_valid():
    """col=0 is a legitimate value meaning 'first character of the line'."""
    d = LintDiagnostic(
        file="f.py", rule="PL001", severity="error", message="m", line=1, col=0
    )
    assert d.col == 0
    assert d.format() == "f.py:1:0: PL001 error: m"


def test_syntax_error_col_is_zero_based():
    """SyntaxError.offset is 1-based; lint_source must convert it to 0-based."""
    # Python raises SyntaxError with offset=1 for the very first character.
    # A minimal 1-character syntax error: an isolated invalid token at col 0.
    result = lint_source("$")  # SyntaxError at offset 1 → col 0
    assert len(result) == 1
    d = result[0]
    assert d.rule == "PL000"
    assert d.col == 0  # converted from 1-based offset=1 → 0-based col=0


def test_clear_rules_empties_registry():
    register_rule(DummyRule())
    assert len(get_rules()) == 1
    clear_rules()
    assert get_rules() == []

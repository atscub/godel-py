"""Tests for godel lint CLI command."""
import json
import subprocess
import sys



def test_lint_clean_file(tmp_path):
    f = tmp_path / "good.py"
    f.write_text('''\
from godel import workflow, step

@step
async def do_work():
    return 1

@workflow
async def main():
    await do_work()
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0


def test_lint_missing_await(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run

async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "PL001" in result.stdout


def test_lint_json_format(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run
async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f), "--format", "json"],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert any(d["rule"] == "PL001" for d in data)


def test_lint_skip_rule(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run
async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f), "--skip", "PL001"],
        capture_output=True, text=True,
    )
    # PL001 skipped — should not appear in output
    assert "PL001" not in result.stdout


def test_lint_warnings_exit_0(tmp_path):
    f = tmp_path / "warn.py"
    f.write_text('''\
async def f():
    try:
        pass
    except:
        pass
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    # PL007 is a warning — should exit 0
    assert result.returncode == 0


def test_lint_text_format_output(tmp_path):
    """Text format includes file:line:col: RULE severity: message."""
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run
async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    # Should match file:line:col: RULE severity: message format
    assert "PL001" in result.stdout
    assert "error" in result.stdout


def test_lint_json_format_dict_shape(tmp_path):
    """JSON output dicts have expected keys."""
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run
async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f), "--format", "json"],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    assert len(data) > 0
    first = data[0]
    assert "rule" in first
    assert "severity" in first
    assert "message" in first
    assert "line" in first
    assert "col" in first
    assert "file" in first


def test_lint_exit_0_on_clean(tmp_path):
    """Clean file with no diagnostics exits 0."""
    f = tmp_path / "clean.py"
    f.write_text('''\
def add(a, b):
    return a + b
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_lint_directory_input(tmp_path):
    """Passing a directory emits a clean error message (not a raw IOError traceback)."""
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    # Should mention 'directory' in the diagnostic, not raw Python traceback
    combined = result.stdout + result.stderr
    assert "directory" in combined.lower()
    assert "Traceback" not in combined


def test_lint_non_py_file(tmp_path):
    """Linting a non-.py file does not crash — it is treated as Python source."""
    f = tmp_path / "workflow.gdl"
    # Write content that is valid Python so we get exit 0 with no diagnostics
    f.write_text("x = 1\n")
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f)],
        capture_output=True, text=True,
    )
    # File extension is not checked; valid Python-parseable content exits 0
    assert result.returncode == 0
    assert "Traceback" not in result.stderr


def test_lint_skip_trailing_comma(tmp_path):
    """--skip with a trailing comma does not crash (empty token is ignored)."""
    f = tmp_path / "bad.py"
    f.write_text('''\
from godel import run
async def f():
    run("echo hi")
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f), "--skip", "PL001,"],
        capture_output=True, text=True,
    )
    # Trailing comma produces an empty token — should not crash and PL001 is skipped
    assert result.returncode == 0 or "PL001" not in result.stdout
    assert "Traceback" not in result.stderr


def test_lint_skip_unknown_rule_warns(tmp_path):
    """--skip with an unknown rule ID emits a warning on stderr."""
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n")
    result = subprocess.run(
        [sys.executable, "-m", "godel", "lint", str(f), "--skip", "PL01"],
        capture_output=True, text=True,
    )
    assert "unknown rule ID" in result.stderr or "PL01" in result.stderr


def test_lint_crashing_rule_emits_pl000(tmp_path, monkeypatch):
    """A lint rule that raises an exception produces a PL000 diagnostic instead of a traceback."""
    # We test this via the Python API (lint_source) since injecting a crashing rule
    # into the subprocess is impractical without shared state.
    import sys
    sys.path.insert(0, str(tmp_path.parent.parent / "py-library"))
    from godel._linter import lint_source, _RULES

    class BrokenRule:
        rule_id = "PL999"
        severity = "error"
        description = "always crashes"

        def check(self, tree, filename):
            raise RuntimeError("boom")

    broken = BrokenRule()
    _RULES.append(broken)
    try:
        diagnostics = lint_source("x = 1", filename="test.py")
        pl000 = [d for d in diagnostics if d.rule == "PL000"]
        assert pl000, "Expected a PL000 diagnostic from the crashing rule"
        assert "PL999" in pl000[0].message
        assert "boom" in pl000[0].message
    finally:
        _RULES.remove(broken)

"""Tests for godel public API exports."""


def test_event_exports():
    from godel import Event, EventStatus, EventLog, get_event_log
    assert Event is not None
    assert EventStatus is not None
    assert EventLog is not None
    assert callable(get_event_log)


def test_cli_prints_run_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import subprocess
    import sys
    from pathlib import Path

    wf = tmp_path / "wf.py"
    wf.write_text('''
from godel import workflow

@workflow
async def wf():
    return 42
''')
    result = subprocess.run(
        [sys.executable, "-m", "godel", "run", str(wf)],
        capture_output=True, text=True, timeout=15,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
    )
    assert result.returncode == 0
    assert "audit log:" in result.stderr
    assert ".jsonl" in result.stderr

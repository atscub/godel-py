"""Tests for PEP 578 audit hook (Layer 3 strict mode).

All tests run in subprocesses because sys.addaudithook is permanent.
"""
import subprocess
import sys
from pathlib import Path

# Find the project root for sys.path
PROJECT_ROOT = str(Path(__file__).parent.parent)


def _run_in_subprocess(code: str) -> subprocess.CompletedProcess:
    """Run code in a child Python process with audit hook installed."""
    wrapper = f"""
import sys
sys.path.insert(0, {PROJECT_ROOT!r})
from godel._strict_audit import install_audit_hook
install_audit_hook()
{code}
"""
    return subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True, text=True, timeout=10,
    )


def test_blocks_file_write():
    result = _run_in_subprocess("open('/tmp/godel_test_write.txt', 'w')")
    assert result.returncode != 0
    assert "cannot write files" in result.stderr


def test_allows_file_read():
    result = _run_in_subprocess("""
import os
f = open(os.path.join(sys.path[0], 'godel', '__init__.py'), 'r')
f.close()
print('ok')
""")
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_allows_privileged_write():
    result = _run_in_subprocess("""
import tempfile, os
from godel._context import _privileged
token = _privileged.set(True)
path = os.path.join(tempfile.gettempdir(), 'godel_test_priv.txt')
f = open(path, 'w')
f.write('test')
f.close()
os.unlink(path)
_privileged.reset(token)
print('ok')
""")
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_idempotent_install():
    result = _run_in_subprocess("""
from godel._strict_audit import install_audit_hook
install_audit_hook()  # second call
print('ok')
""")
    assert result.returncode == 0
    assert "ok" in result.stdout

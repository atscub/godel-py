"""Layer 3: PEP 578 audit hook for strict mode."""
from __future__ import annotations

import sys

from godel._context import _privileged

BLOCKED_EVENTS: set[str] = {
    "socket.connect", "socket.bind",
    "urllib.Request",
    "subprocess.Popen",
    "os.system", "os.exec", "os.fork",
    "ctypes.dlopen",
}

_installed = False


def _audit_hook(event: str, args):
    """PEP 578 audit hook for strict mode."""
    if _privileged.get():
        return

    if event == "open":
        if len(args) >= 2:
            path, mode = args[0], args[1]
            if mode and any(c in str(mode) for c in "wxa+"):
                raise PermissionError(
                    f"godel strict: workflow cannot write files: {path}"
                )
    elif event in BLOCKED_EVENTS:
        raise PermissionError(
            f"godel strict: workflow cannot perform: {event}"
        )


def install_audit_hook():
    """Install the PEP 578 audit hook. PERMANENT -- cannot be removed."""
    global _installed
    if _installed:
        return
    sys.addaudithook(_audit_hook)
    _installed = True

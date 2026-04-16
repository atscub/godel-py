"""Layer 1: AST pre-scan for banned calls and modules."""
from __future__ import annotations

import ast

from godel._exceptions import StrictViolation

BANNED_ATTR_CALLS: set[tuple[str, str]] = {
    ("time", "time"), ("time", "monotonic"), ("time", "sleep"),
    ("asyncio", "sleep"),
    ("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"),
    ("random", "random"), ("random", "choice"), ("random", "randint"),
    ("random", "uniform"), ("random", "shuffle"),
    ("uuid", "uuid1"), ("uuid", "uuid4"),
}

BANNED_MODULES: set[str] = {
    "requests", "httpx", "urllib.request", "socket",
    "threading", "multiprocessing",
}


class _StrictVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[StrictViolation] = []
        # For ``import X`` / ``import X as Y``: local_name -> module_name (str)
        # For ``from M import attr`` / ``from M import attr as local``:
        #   local_name -> (source_module, original_attr) (tuple[str, str])
        self._imported_names: dict[str, str | tuple[str, str]] = {}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name in BANNED_MODULES:
                self.violations.append(StrictViolation(
                    file=self.filename, line=node.lineno, col=node.col_offset,
                    message=f"banned module import: {alias.name}",
                    layer="ast",
                ))
            local_name = alias.asname or alias.name
            self._imported_names[local_name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module and node.module in BANNED_MODULES:
            self.violations.append(StrictViolation(
                file=self.filename, line=node.lineno, col=node.col_offset,
                message=f"banned module import: {node.module}",
                layer="ast",
            ))
        if node.module:
            for alias in node.names:
                local_name = alias.asname or alias.name
                original_attr = alias.name
                # Store the (source_module, original_attr) tuple so that aliased
                # bare-name calls like ``aio_sleep(1)`` can be resolved back to
                # (asyncio, sleep) and checked against BANNED_ATTR_CALLS.
                self._imported_names[local_name] = (node.module, original_attr)
        self.generic_visit(node)

    def _record_banned(self, node: ast.Call, module_name: str, attr: str) -> None:
        hint = "use godel.sleep instead" if attr == "sleep" else "use godel.det instead"
        self.violations.append(StrictViolation(
            file=self.filename, line=node.lineno, col=node.col_offset,
            message=f"banned call: {module_name}.{attr}() — {hint}",
            layer="ast",
        ))

    def visit_Call(self, node: ast.Call):
        # Module.attr form: e.g. ``time.sleep(1)`` or ``aio.sleep(1)`` (aliased).
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            module_alias = node.func.value.id
            entry = self._imported_names.get(module_alias, module_alias)
            # entry is a plain module name string for ``import X [as Y]``
            module_name = entry if isinstance(entry, str) else module_alias
            pair = (module_name, node.func.attr)
            if pair in BANNED_ATTR_CALLS:
                self._record_banned(node, module_name, node.func.attr)
        # Bare-Name form: e.g. ``sleep(1)`` after ``from asyncio import sleep``
        # or ``aio_sleep(1)`` after ``from asyncio import sleep as aio_sleep``.
        # _imported_names stores (source_module, original_attr) tuples for
        # from-imports, so we always check against the canonical attr name even
        # when the user has introduced a local alias.
        elif isinstance(node.func, ast.Name):
            entry = self._imported_names.get(node.func.id)
            if isinstance(entry, tuple):
                source_module, original_attr = entry
                pair = (source_module, original_attr)
                if pair in BANNED_ATTR_CALLS:
                    self._record_banned(node, source_module, original_attr)
        self.generic_visit(node)


def scan_file(path: str, *, raise_on_violation: bool = True) -> list[StrictViolation]:
    """Scan a Python file for banned operations. Returns violations list."""
    from godel._exceptions import GodelStrictError

    with open(path) as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    visitor = _StrictVisitor(path)
    visitor.visit(tree)
    if visitor.violations and raise_on_violation:
        raise GodelStrictError(visitor.violations)
    return visitor.violations


def scan_source(source: str, filename: str = "<string>") -> list[StrictViolation]:
    """Scan source code string. For testing."""
    tree = ast.parse(source, filename=filename)
    visitor = _StrictVisitor(filename)
    visitor.visit(tree)
    return visitor.violations

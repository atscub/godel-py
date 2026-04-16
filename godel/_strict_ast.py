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
        self._imported_names: dict[str, str] = {}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name in BANNED_MODULES:
                self.violations.append(StrictViolation(
                    file=self.filename, line=node.lineno, col=node.col_offset,
                    message=f"banned module import: {alias.name}",
                    layer="ast",
                ))
            name = alias.asname or alias.name
            self._imported_names[name] = alias.name
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
                name = alias.asname or alias.name
                self._imported_names[name] = node.module
        self.generic_visit(node)

    _SLEEP_PAIRS: frozenset[tuple[str, str]] = frozenset({
        ("time", "sleep"), ("asyncio", "sleep"),
    })

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            module_alias = node.func.value.id
            module_name = self._imported_names.get(module_alias, module_alias)
            pair = (module_name, node.func.attr)
            if pair in BANNED_ATTR_CALLS:
                if pair in self._SLEEP_PAIRS:
                    hint = "use godel.sleep instead"
                else:
                    hint = "use godel.det instead"
                self.violations.append(StrictViolation(
                    file=self.filename, line=node.lineno, col=node.col_offset,
                    message=f"banned call: {module_name}.{node.func.attr}() — {hint}",
                    layer="ast",
                ))
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

"""Workflow linter — static analysis for common mistakes."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable


@dataclass
class LintDiagnostic:
    """A single lint diagnostic, matching the DSL linter output shape.

    Column convention: all columns are **0-based** (i.e. ``col=0`` means the
    first character of the line).  ``col=None`` means the column is unknown or
    not applicable (e.g. file-level errors).

    Note: ``ast.SyntaxError.offset`` is 1-based; ``lint_source`` converts it to
    0-based before storing it here.
    """

    file: str
    rule: str
    severity: Literal["error", "warning"]
    message: str
    line: int
    col: int | None = None

    def __post_init__(self) -> None:
        if self.severity not in ("error", "warning"):
            raise ValueError(
                f"LintDiagnostic.severity must be 'error' or 'warning', got {self.severity!r}"
            )

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "col": self.col,
        }

    def format(self) -> str:
        """Format as file:line:col: RULE severity: message.

        ``col`` is omitted (shown as empty) when unknown (``None``).
        """
        col_str = "" if self.col is None else str(self.col)
        return f"{self.file}:{self.line}:{col_str}: {self.rule} {self.severity}: {self.message}"


@runtime_checkable
class LintRule(Protocol):
    """Protocol for lint rules. Each rule inspects a parsed AST.

    Marked ``@runtime_checkable`` so that ``isinstance(obj, LintRule)`` works
    for validation in ``register_rule``.
    """

    rule_id: str
    severity: str  # "error" | "warning"
    description: str

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]: ...


# Global rule registry
_RULES: list[LintRule] = []


def register_rule(rule: LintRule) -> None:
    """Add a rule to the global registry.

    Raises ``TypeError`` if *rule* does not satisfy the ``LintRule`` Protocol
    (i.e. missing ``rule_id``, ``severity``, ``description``, or ``check``).
    """
    if not isinstance(rule, LintRule):
        raise TypeError(
            f"register_rule() expects a LintRule instance, got {type(rule)!r}. "
            "Rule must have rule_id, severity, description, and check() attributes."
        )
    _RULES.append(rule)


def clear_rules() -> None:
    """Remove all rules from the global registry.

    Provided as a public alternative to directly mutating ``_RULES`` — use this
    in test fixtures instead of importing and clearing ``_RULES`` directly.
    This is also safer for ``pytest-xdist`` when used with a process-level
    registry snapshot/restore pattern.
    """
    _RULES.clear()


def get_rules() -> list[LintRule]:
    """Return all registered rules."""
    return list(_RULES)


def lint_file(path: str, *, skip_rules: set[str] | None = None) -> list[LintDiagnostic]:
    """Parse a Python file and run all registered lint rules.

    Args:
        path: Path to the Python file to lint.
        skip_rules: Set of rule_ids to skip (e.g. {"PL003", "PL007"}).

    Returns:
        List of diagnostics sorted by line number.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [
            LintDiagnostic(
                file=path,
                rule="PL000",
                severity="error",
                message=f"IOError: file not found: {path}",
                line=0,
                col=None,
            )
        ]
    except IsADirectoryError:
        return [
            LintDiagnostic(
                file=path,
                rule="PL000",
                severity="error",
                message=f"'{path}' is a directory, not a file",
                line=0,
                col=None,
            )
        ]
    except (PermissionError, OSError) as e:
        return [
            LintDiagnostic(
                file=path,
                rule="PL000",
                severity="error",
                message=f"IOError: {e}",
                line=0,
                col=None,
            )
        ]
    except UnicodeDecodeError as e:
        return [
            LintDiagnostic(
                file=path,
                rule="PL000",
                severity="error",
                message=f"UnicodeDecodeError: {e.reason} at byte offset {e.start}",
                line=0,
                col=None,
            )
        ]
    return lint_source(source, filename=path, skip_rules=skip_rules)


def lint_source(
    source: str,
    filename: str = "<string>",
    *,
    skip_rules: set[str] | None = None,
) -> list[LintDiagnostic]:
    """Lint source code directly (useful for testing).

    Args:
        source: Python source code as a string.
        filename: Filename label for diagnostics (default: "<string>").
        skip_rules: Set of rule_ids to skip.

    Returns:
        List of diagnostics sorted by line number.
    """
    skip = skip_rules or set()
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        # SyntaxError.offset is 1-based; convert to 0-based to stay consistent
        # with all other diagnostics (which use ast node col_offset, 0-based).
        raw_offset = e.offset  # may be None
        col: int | None = (raw_offset - 1) if raw_offset is not None else None
        return [
            LintDiagnostic(
                file=filename,
                rule="PL000",
                severity="error",
                message=f"SyntaxError: {e.msg}",
                line=e.lineno or 0,
                col=col,
            )
        ]

    diagnostics: list[LintDiagnostic] = []
    for rule in list(_RULES):  # snapshot to guard against register_rule() during iteration
        if rule.rule_id in skip:
            continue
        try:
            diagnostics.extend(rule.check(tree, filename))
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(
                LintDiagnostic(
                    file=filename,
                    rule="PL000",
                    severity="error",
                    message=f"internal error in rule {rule.rule_id}: {exc}",
                    line=0,
                    col=None,
                )
            )

    # Sort by (line, col); treat None col as -1 so unknown-col items sort first
    # within their line.
    diagnostics.sort(key=lambda d: (d.line, d.col if d.col is not None else -1))
    return diagnostics


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _is_decorator_named(dec: ast.expr, name: str) -> bool:
    """Check if a decorator is @name or @name(...)."""
    if isinstance(dec, ast.Name):
        return dec.id == name
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
        return dec.func.id == name
    return False


# Godel async primitives that must be awaited.
# NOTE: only bare Name calls (e.g. `run(...)`) are checked — method calls
# like `obj.run(...)` or `subprocess.run(...)` are excluded by PL001 to avoid
# false positives.  `print` and `input` are intentionally excluded here because
# they shadow Python builtins and would produce false positives on any normal
# print/input call.  The godel equivalents are accessed via the `godel` module
# and therefore appear as attribute calls, not bare names.
_GODEL_ASYNC_NAMES: frozenset[str] = frozenset({"run", "rewind", "parallel"})


def _contains_godel_primitive(node: ast.AST) -> bool:
    """Check if the *direct* body of a function contains godel primitive calls.

    Uses a manual BFS that stops descending into nested ``FunctionDef`` /
    ``AsyncFunctionDef`` / ``Lambda`` nodes.  That way a nested def or lambda
    that calls ``run()`` does NOT cause PL002 to fire on the outer function —
    the inner def will be visited separately by the rule's own ``ast.walk``
    loop (lambdas are not visited separately since they are rarely @step
    candidates, so they act as a hard boundary).
    """
    queue: list[ast.AST] = [node]
    while queue:
        current = queue.pop()
        if isinstance(current, ast.Call):
            if _get_call_name(current) in _GODEL_ASYNC_NAMES:
                return True
        for child in ast.iter_child_nodes(current):
            # Stop at nested function / lambda boundaries (but still process
            # the root node itself).
            if child is not node and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            queue.append(child)
    return False


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Return a mapping of alias → module_name for ``import X as alias`` statements.

    For example, ``import random as r`` produces ``{"r": "random"}``.
    Also handles ``import datetime.datetime as dt`` → ``{"dt": "datetime"}``.
    Sub-module imports like ``import datetime`` (no alias) are not included
    because the local name equals the module name and PL003 already handles that.

    Also handles ``from X import Y as alias`` — for example,
    ``from datetime import datetime as dt`` produces ``{"dt": "datetime"}``,
    mapping the alias to the parent module so that ``dt.now()`` is still
    detected by PL003 as a ``datetime.now`` call.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    # Map alias → top-level module name (e.g. "r" → "random")
                    top_module = alias.name.split(".")[0]
                    aliases[alias.asname] = top_module
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top_module = module.split(".")[0]
            for alias in node.names:
                if alias.asname:
                    # e.g. ``from datetime import datetime as dt``
                    # The attribute call ``dt.now()`` should be treated as
                    # ``datetime.now()`` — map the alias to the top-level module.
                    aliases[alias.asname] = top_module
    return aliases


# ── Rule implementations ──────────────────────────────────────────────────────


class PL001_MissingAwait:
    """Detect godel async primitives called without await."""

    rule_id = "PL001"
    severity = "error"
    description = "godel async primitive called without await"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []

        # Collect the id() of every Call node that is directly inside an Await
        awaited_call_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                awaited_call_ids.add(id(node.value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and id(node) not in awaited_call_ids:
                # Only flag bare Name calls (e.g. `run(...)`), not method calls
                # like `obj.run(...)` — those are not godel primitives.
                if not isinstance(node.func, ast.Name):
                    continue
                name = node.func.id
                if name in _GODEL_ASYNC_NAMES:
                    diagnostics.append(
                        LintDiagnostic(
                            file=filename,
                            rule=self.rule_id,
                            severity=self.severity,
                            message=f"'{name}()' is async and must be awaited",
                            line=node.lineno,
                            col=node.col_offset,
                        )
                    )
        return diagnostics


class PL002_MissingStep:
    """Functions containing godel primitives but missing @step/@workflow decorator."""

    rule_id = "PL002"
    severity = "warning"
    description = "function with godel primitives should use @step"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            has_step = any(_is_decorator_named(d, "step") for d in node.decorator_list)
            has_workflow = any(_is_decorator_named(d, "workflow") for d in node.decorator_list)
            if has_step or has_workflow:
                continue

            if _contains_godel_primitive(node):
                diagnostics.append(
                    LintDiagnostic(
                        file=filename,
                        rule=self.rule_id,
                        severity=self.severity,
                        message=(
                            f"function '{node.name}' uses godel primitives but lacks @step decorator"
                        ),
                        line=node.lineno,
                        col=node.col_offset,
                    )
                )
        return diagnostics


class PL003_NonDeterminism:
    """Calls to known non-deterministic stdlib functions."""

    rule_id = "PL003"
    severity = "error"
    description = "non-deterministic operation detected"

    _BANNED: frozenset[tuple[str, str]] = frozenset(
        {
            ("datetime", "now"),
            ("datetime", "today"),
            ("datetime", "utcnow"),
            ("time", "time"),
            ("time", "monotonic"),
            ("random", "random"),
            ("random", "choice"),
            ("random", "randint"),
            ("random", "uniform"),
            ("random", "shuffle"),
            ("uuid", "uuid1"),
            ("uuid", "uuid4"),
        }
    )

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []
        # Build alias map so ``import random as r; r.random()`` is caught.
        # Maps local_name → canonical_module_name.
        aliases = _collect_import_aliases(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
            ):
                local_name = node.func.value.id
                method = node.func.attr
                # Resolve alias → canonical module name; if no alias, use as-is.
                canonical = aliases.get(local_name, local_name)
                pair = (canonical, method)
                if pair in self._BANNED:
                    # Report using the name as written in source for clarity.
                    display = f"{local_name}.{method}"
                    diagnostics.append(
                        LintDiagnostic(
                            file=filename,
                            rule=self.rule_id,
                            severity=self.severity,
                            message=(
                                f"'{display}()' is non-deterministic; "
                                "use godel.det equivalents"
                            ),
                            line=node.lineno,
                            col=node.col_offset,
                        )
                    )
        return diagnostics


class PL004_RunInLoop:
    """run() inside a loop without idempotent=True."""

    rule_id = "PL004"
    severity = "warning"
    description = "run() in loop without idempotent=True may block rewind"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []
        # Track Call node ids already reported so nested loops don't emit
        # duplicate diagnostics for the same run() call.
        reported_ids: set[int] = set()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue

            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if id(child) in reported_ids:
                    continue
                if _get_call_name(child) != "run":
                    continue
                has_idempotent = any(
                    kw.arg == "idempotent"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in child.keywords
                )
                if not has_idempotent:
                    reported_ids.add(id(child))
                    diagnostics.append(
                        LintDiagnostic(
                            file=filename,
                            rule=self.rule_id,
                            severity=self.severity,
                            message="run() inside a loop without idempotent=True will block rewind",
                            line=child.lineno,
                            col=child.col_offset,
                        )
                    )
        return diagnostics


class PL005_MissingAsync:
    """@workflow or @step on a synchronous def (not async def)."""

    rule_id = "PL005"
    severity = "error"
    description = "@workflow/@step requires async def"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []
        for node in ast.walk(tree):
            # Only plain FunctionDef — AsyncFunctionDef is fine
            if not isinstance(node, ast.FunctionDef):
                continue
            # Collect which godel decorators are present (workflow takes priority
            # for the message; either one is enough to trigger the rule).
            has_workflow = any(_is_decorator_named(d, "workflow") for d in node.decorator_list)
            has_step = any(_is_decorator_named(d, "step") for d in node.decorator_list)
            if not (has_workflow or has_step):
                continue
            # Emit a single diagnostic per function even if both decorators
            # are present (NIT: PL005 double-fires on @step @workflow combo).
            dec_name = "workflow" if has_workflow else "step"
            diagnostics.append(
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message=f"@{dec_name} requires 'async def', not 'def'",
                    line=node.lineno,
                    col=node.col_offset,
                )
            )
        return diagnostics


class PL006_WorkflowWithoutStep:
    """@workflow function that never calls any @step-decorated function."""

    rule_id = "PL006"
    severity = "warning"
    description = "@workflow without any @step calls"

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []

        # Collect names of @step-decorated functions defined in this module.
        step_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if _is_decorator_named(dec, "step"):
                        step_names.add(node.name)

        # Collect all names imported into this module (via ``from X import Y``
        # or ``import X as Y``).  These *may* be @step functions defined in
        # another module — we cannot verify cross-module step membership
        # statically.
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name.split(".")[0])

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_decorator_named(d, "workflow") for d in node.decorator_list):
                continue

            # Per-workflow analysis: determine whether any call inside this
            # workflow body targets an imported name that is NOT a locally
            # defined @step.  If so, we cannot rule out that it is a cross-
            # module @step, so we suppress PL006 entirely for this workflow
            # (setting has_unknown_cross_module_call = True).
            #
            # This prevents the false-negative described in the critical review:
            # using ``reachable_steps = step_names | imported_names`` would
            # silence PL006 for any workflow that calls *any* imported helper,
            # even if that helper is not a step.  The new logic only suppresses
            # when there is genuinely ambiguous cross-module call.
            has_local_step_call = False
            has_unknown_cross_module_call = False

            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                call_name = _get_call_name(child)
                if not call_name:
                    continue
                if call_name in step_names:
                    has_local_step_call = True
                    break
                if call_name in imported_names and call_name not in step_names:
                    # This call targets an imported name that is not a locally
                    # defined @step — it *might* be a cross-module @step.
                    has_unknown_cross_module_call = True

            if has_local_step_call:
                # Workflow definitely calls a known local @step — OK.
                continue

            if has_unknown_cross_module_call:
                # At least one call goes to an imported name whose @step status
                # cannot be verified statically — suppress PL006 for this
                # workflow rather than risk a false positive.
                continue

            # No local @step call and no ambiguous imported call — fire PL006.
            diagnostics.append(
                LintDiagnostic(
                    file=filename,
                    rule=self.rule_id,
                    severity=self.severity,
                    message=f"@workflow '{node.name}' never calls any @step function",
                    line=node.lineno,
                    col=node.col_offset,
                )
            )
        return diagnostics


class PL007_BareExcept:
    """bare except: or except Exception: that may swallow godel control flow signals."""

    rule_id = "PL007"
    severity = "warning"
    description = "bare except may swallow godel exceptions (WorkflowFail, RewindSignal)"

    # Exception names broad enough to swallow godel control-flow signals
    _BROAD_EXCEPTIONS: frozenset[str] = frozenset({"Exception", "BaseException"})

    def _handler_is_broad(self, node: ast.ExceptHandler) -> tuple[bool, str]:
        """Return (is_broad, reason) for an ExceptHandler node."""
        # bare except:
        if node.type is None:
            return True, "bare 'except:' may swallow WorkflowFail or RewindSignal"

        # except Exception: / except BaseException:
        if isinstance(node.type, ast.Name) and node.type.id in self._BROAD_EXCEPTIONS:
            return (
                True,
                f"'except {node.type.id}' may swallow WorkflowFail or RewindSignal; "
                "consider catching specific types",
            )

        # except (Exception, SomeOther, ...): — flag if any element is broad
        if isinstance(node.type, ast.Tuple):
            broad = [
                elt.id
                for elt in node.type.elts
                if isinstance(elt, ast.Name) and elt.id in self._BROAD_EXCEPTIONS
            ]
            if broad:
                names = ", ".join(broad)
                return (
                    True,
                    f"'except ({names}, ...)' may swallow WorkflowFail or RewindSignal; "
                    "consider catching specific types",
                )

        return False, ""

    def check(self, tree: ast.AST, filename: str) -> list[LintDiagnostic]:
        diagnostics: list[LintDiagnostic] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            is_broad, reason = self._handler_is_broad(node)
            if is_broad:
                diagnostics.append(
                    LintDiagnostic(
                        file=filename,
                        rule=self.rule_id,
                        severity=self.severity,
                        message=reason,
                        line=node.lineno,
                        col=node.col_offset,
                    )
                )
        return diagnostics


# ── Auto-register built-in rules ──────────────────────────────────────────────


def _register_builtin_rules() -> None:
    for cls in [
        PL001_MissingAwait,
        PL002_MissingStep,
        PL003_NonDeterminism,
        PL004_RunInLoop,
        PL005_MissingAsync,
        PL006_WorkflowWithoutStep,
        PL007_BareExcept,
    ]:
        register_rule(cls())


_register_builtin_rules()

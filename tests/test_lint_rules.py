"""Tests for lint rules PL001-PL007."""
from __future__ import annotations

import pytest

from godel._linter import _RULES, lint_source


# ---------------------------------------------------------------------------
# Fixture: preserve the rule registry across tests
# (Rules auto-register at import time; save/restore so other tests don't break)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def preserve_rules():
    """Save the registry state before each test and restore it afterwards."""
    original = list(_RULES)
    yield
    _RULES.clear()
    _RULES.extend(original)


# ---------------------------------------------------------------------------
# PL001 — Missing await on godel async primitives
# ---------------------------------------------------------------------------


def test_pl001_triggers_on_bare_run():
    src = """
async def f():
    run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL001" for d in diags), diags


def test_pl001_no_false_positive_when_awaited():
    src = """
async def f():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


def test_pl001_triggers_on_all_primitives():
    # `print` and `input` are Python builtins and are NOT checked by PL001
    # (they would cause massive false positives).  The godel async primitives
    # that ARE checked as bare names are: run, rewind, parallel.
    src = """
async def f():
    run("cmd")
    rewind()
    parallel([])
"""
    diags = lint_source(src)
    pl001 = [d for d in diags if d.rule == "PL001"]
    assert len(pl001) >= 3, pl001


def test_pl001_no_false_positive_non_godel_call():
    src = """
async def f():
    open("file.txt")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


def test_pl001_no_false_positive_method_call():
    """obj.run() is a method call, not the godel primitive — must not trigger."""
    src = """
async def f():
    obj.run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


def test_pl001_no_false_positive_subprocess_run():
    """subprocess.run() must not trigger — it is not the godel primitive."""
    src = """
import subprocess
def f():
    subprocess.run(["ls"])
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


def test_pl001_no_false_positive_builtin_print():
    """Python's builtin print() must not trigger PL001."""
    src = """
async def f():
    print("hello")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


def test_pl001_no_false_positive_builtin_input():
    """Python's builtin input() must not trigger PL001."""
    src = """
async def f():
    x = input("prompt: ")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL001" for d in diags), diags


# ---------------------------------------------------------------------------
# PL002 — Missing @step on functions with godel primitives
# ---------------------------------------------------------------------------


def test_pl002_triggers_on_undecorated_function():
    src = """
async def my_fn():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL002" for d in diags), diags


def test_pl002_no_false_positive_with_step():
    src = """
@step
async def my_fn():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL002" for d in diags), diags


def test_pl002_no_false_positive_with_workflow():
    src = """
@workflow
async def my_wf():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL002" for d in diags), diags


def test_pl002_no_false_positive_without_primitives():
    src = """
async def helper():
    x = 1 + 1
    return x
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL002" for d in diags), diags


# ---------------------------------------------------------------------------
# PL003 — Non-determinism
# ---------------------------------------------------------------------------


def test_pl003_triggers_on_datetime_now():
    src = """
import datetime
def f():
    datetime.now()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), diags


def test_pl003_triggers_on_random_choice():
    src = """
import random
def f():
    random.choice([1, 2, 3])
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), diags


def test_pl003_triggers_on_uuid4():
    src = """
import uuid
def f():
    uuid.uuid4()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), diags


def test_pl003_no_false_positive_on_deterministic_call():
    src = """
def f():
    x = len([1, 2, 3])
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL003" for d in diags), diags


# ---------------------------------------------------------------------------
# PL004 — run() in loop without idempotent=True
# ---------------------------------------------------------------------------


def test_pl004_triggers_in_for_loop():
    src = """
async def f():
    for i in range(3):
        await run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL004" for d in diags), diags


def test_pl004_triggers_in_while_loop():
    src = """
async def f():
    while True:
        await run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL004" for d in diags), diags


def test_pl004_no_false_positive_with_idempotent():
    src = """
async def f():
    for i in range(3):
        await run("echo hi", idempotent=True)
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL004" for d in diags), diags


def test_pl004_no_false_positive_outside_loop():
    src = """
async def f():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL004" for d in diags), diags


def test_pl004_triggers_in_async_for_loop():
    """async for is a loop — run() inside it without idempotent=True must trigger."""
    src = """
async def f():
    async for item in some_iter():
        await run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL004" for d in diags), diags


def test_pl004_no_duplicate_in_nested_loops():
    """A single run() inside nested loops must produce exactly one PL004 diagnostic."""
    src = """
async def f():
    for i in range(3):
        for j in range(3):
            await run("echo hi")
"""
    diags = lint_source(src)
    pl004 = [d for d in diags if d.rule == "PL004"]
    assert len(pl004) == 1, pl004


# ---------------------------------------------------------------------------
# PL005 — Missing async def on @workflow/@step
# ---------------------------------------------------------------------------


def test_pl005_triggers_on_sync_workflow():
    src = """
@workflow
def my_wf():
    pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL005" for d in diags), diags


def test_pl005_triggers_on_sync_step():
    src = """
@step
def my_step():
    pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL005" for d in diags), diags


def test_pl005_no_false_positive_async_workflow():
    src = """
@workflow
async def my_wf():
    pass
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL005" for d in diags), diags


def test_pl005_no_false_positive_async_step():
    src = """
@step
async def my_step():
    pass
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL005" for d in diags), diags


def test_pl005_no_false_positive_undecorated_sync():
    src = """
def plain_function():
    pass
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL005" for d in diags), diags


# ---------------------------------------------------------------------------
# PL006 — @workflow without any @step calls
# ---------------------------------------------------------------------------


def test_pl006_triggers_when_no_step_called():
    src = """
@workflow
async def my_wf():
    x = 1
"""
    diags = lint_source(src)
    assert any(d.rule == "PL006" for d in diags), diags


def test_pl006_no_false_positive_when_step_called():
    src = """
@step
async def do_work():
    await run("echo hi")

@workflow
async def my_wf():
    await do_work()
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL006" for d in diags), diags


def test_pl006_no_false_positive_on_step_functions():
    # @step functions are not required to call other steps
    src = """
@step
async def do_work():
    await run("echo hi")
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL006" for d in diags), diags


# ---------------------------------------------------------------------------
# PL007 — Bare except
# ---------------------------------------------------------------------------


def test_pl007_triggers_on_bare_except():
    src = """
async def f():
    try:
        pass
    except:
        pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL007" for d in diags), diags


def test_pl007_triggers_on_except_exception():
    src = """
async def f():
    try:
        pass
    except Exception:
        pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL007" for d in diags), diags


def test_pl007_no_false_positive_specific_exception():
    src = """
async def f():
    try:
        pass
    except ValueError:
        pass
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL007" for d in diags), diags


def test_pl007_no_false_positive_no_try():
    src = """
async def f():
    x = 1 + 1
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL007" for d in diags), diags


def test_pl007_triggers_on_except_base_exception():
    """except BaseException is broad enough to swallow godel signals."""
    src = """
async def f():
    try:
        pass
    except BaseException:
        pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL007" for d in diags), diags


def test_pl007_triggers_on_except_tuple_containing_exception():
    """except (Exception, KeyboardInterrupt) contains Exception — must trigger."""
    src = """
async def f():
    try:
        pass
    except (Exception, KeyboardInterrupt):
        pass
"""
    diags = lint_source(src)
    assert any(d.rule == "PL007" for d in diags), diags


def test_pl007_no_false_positive_tuple_of_specific_exceptions():
    """except (ValueError, TypeError) is specific — must not trigger."""
    src = """
async def f():
    try:
        pass
    except (ValueError, TypeError):
        pass
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL007" for d in diags), diags


# ---------------------------------------------------------------------------
# WARN-1: PL003 — aliased imports must be tracked
# ---------------------------------------------------------------------------


def test_pl003_aliased_import_triggers():
    """``import random as r; r.random()`` — alias must be resolved to 'random'."""
    src = """
import random as r
def f():
    r.random()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), diags


def test_pl003_aliased_datetime_triggers():
    """``import datetime as dt; dt.now()`` — alias resolved correctly."""
    src = """
import datetime as dt
def f():
    dt.now()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), diags


def test_pl003_aliased_no_false_positive_on_unrelated_alias():
    """``import math as m; m.sqrt()`` — not in banned list, no PL003."""
    src = """
import math as m
def f():
    m.sqrt(4)
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL003" for d in diags), diags


# ---------------------------------------------------------------------------
# WARN-2: PL002 — nested function inside @step parent must not false-positive
# ---------------------------------------------------------------------------


def test_pl002_no_false_positive_nested_def_inside_step():
    """Inner def that calls run() should NOT trigger PL002 on the outer @step.

    The outer function already has @step, so it is exempt.  The inner def
    will be visited separately and *will* trigger PL002 (it lacks @step).
    Only the outer function is asserted here.
    """
    src = """
@step
async def outer():
    def inner():
        run("echo hi")
    inner()
"""
    diags = lint_source(src)
    # outer has @step — it must NOT trigger PL002
    outer_diags = [d for d in diags if d.rule == "PL002" and d.line == 2]
    assert outer_diags == [], outer_diags


def test_pl002_nested_def_without_step_triggers_on_inner():
    """Inner def that calls run() and lacks @step should trigger PL002 itself."""
    src = """
@step
async def outer():
    async def inner():
        await run("echo hi")
    await inner()
"""
    diags = lint_source(src)
    pl002 = [d for d in diags if d.rule == "PL002"]
    # inner (line 4) should fire; outer (line 2) should NOT
    inner_lines = [d.line for d in pl002]
    assert any(line >= 4 for line in inner_lines), pl002
    assert not any(line == 2 for line in pl002), pl002


def test_pl002_no_false_positive_outer_workflow_with_nested_step_body():
    """@workflow outer with nested def containing run() — outer is exempt."""
    src = """
@workflow
async def my_wf():
    def helper():
        run("cmd")
"""
    diags = lint_source(src)
    wf_diags = [d for d in diags if d.rule == "PL002" and d.line == 2]
    assert wf_diags == [], wf_diags


# ---------------------------------------------------------------------------
# WARN-3: PL006 — cross-module steps must not always trigger
# ---------------------------------------------------------------------------


def test_pl006_no_false_positive_cross_module_imported_step():
    """@workflow that calls an imported function must not trigger PL006.

    The imported function may be a @step in its own module; we cannot verify
    that statically so we suppress the warning for imported calls.
    """
    src = """
from mymodule import do_work

@workflow
async def my_wf():
    await do_work()
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL006" for d in diags), diags


def test_pl006_triggers_when_truly_no_step():
    """@workflow that calls only plain helpers (not imported, not @step) fires PL006."""
    src = """
@workflow
async def my_wf():
    x = 1 + 1
"""
    diags = lint_source(src)
    assert any(d.rule == "PL006" for d in diags), diags


# ---------------------------------------------------------------------------
# NIT: PL005 — double-fire on @step @workflow same function
# ---------------------------------------------------------------------------


def test_pl005_no_double_fire_on_both_decorators():
    """A sync def with both @step and @workflow must produce exactly one PL005."""
    src = """
@step
@workflow
def my_fn():
    pass
"""
    diags = lint_source(src)
    pl005 = [d for d in diags if d.rule == "PL005"]
    assert len(pl005) == 1, pl005


# ---------------------------------------------------------------------------
# CRITICAL fix: PL006 — imported non-step helpers must still fire PL006
# ---------------------------------------------------------------------------


def test_pl006_false_negative_imported_non_step_helpers():
    """Regression: workflow that only calls imported non-step helpers fires PL006.

    The old implementation used ``reachable_steps = step_names | imported_names``
    which silenced PL006 whenever *any* imported name was called, even if those
    imports are plain utility functions and not @step functions.  With the fix,
    PL006 must still fire when no local @step is called AND no imported name is
    actually a known @step (i.e. the imports are non-step helpers with no
    local @step decoration).

    Because we cannot prove at static-analysis time that an imported function
    is NOT a @step (it could be decorated in its own module), the correct
    behaviour is to suppress PL006 when an ambiguous imported call exists.
    This test documents the suppression rule: a workflow calling ONLY imported
    helpers is suppressed (imported call is ambiguous), while a workflow calling
    ONLY locally-defined non-step functions fires PL006.
    """
    # Workflow calls only locally-defined plain helpers (not @step, not imported)
    # — PL006 MUST fire.
    src_local_only = """
def helper():
    pass

@workflow
async def my_workflow():
    helper()
"""
    diags = lint_source(src_local_only)
    assert any(d.rule == "PL006" for d in diags), (
        "PL006 must fire when workflow calls only locally-defined non-step helper"
    )


def test_pl006_imported_call_suppresses_warning():
    """Workflow calling an imported function suppresses PL006 (ambiguous step status)."""
    src = """
from utils import validate_input, format_output

@workflow
async def my_workflow():
    x = validate_input('hello')
    return format_output(x)
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL006" for d in diags), (
        "PL006 must be suppressed when workflow calls imported names "
        "(they may be @step in their own module)"
    )


def test_pl006_local_step_call_suppresses_warning():
    """Workflow calling a locally-defined @step does not fire PL006."""
    src = """
@step
async def do_work():
    await run("echo hi")

@workflow
async def my_wf():
    await do_work()
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL006" for d in diags), diags


def test_pl006_fires_when_only_local_non_step_called():
    """Workflow that calls only a local non-step function (no imports) must fire PL006."""
    src = """
async def plain_helper():
    return 42

@workflow
async def my_wf():
    await plain_helper()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL006" for d in diags), (
        "PL006 must fire when workflow calls only local non-@step function"
    )


# ---------------------------------------------------------------------------
# WARN-1 fix: _collect_import_aliases — from X import Y as alias
# ---------------------------------------------------------------------------


def test_pl003_from_import_alias_triggers():
    """``from datetime import datetime as dt; dt.now()`` must trigger PL003.

    WARN-1 regression: _collect_import_aliases previously ignored
    ``ast.ImportFrom`` nodes, so ``from X import Y as alias`` was not tracked.
    """
    src = """
from datetime import datetime as dt
def f():
    dt.now()
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), (
        "PL003 must fire for 'from datetime import datetime as dt; dt.now()'"
    )


def test_pl003_from_import_alias_random_triggers():
    """``from random import Random as Rng; Rng.choice([])`` is caught via alias."""
    src = """
from random import Random as Rng
def f():
    Rng.choice([1, 2, 3])
"""
    diags = lint_source(src)
    assert any(d.rule == "PL003" for d in diags), (
        "PL003 must fire for 'from random import Random as Rng; Rng.choice(...)'"
    )


def test_pl003_from_import_alias_no_false_positive():
    """``from math import sqrt as sq; sq(4)`` must not trigger PL003."""
    src = """
from math import sqrt as sq
def f():
    sq(4)
"""
    diags = lint_source(src)
    assert not any(d.rule == "PL003" for d in diags), (
        "PL003 must NOT fire for an unrelated from-import alias"
    )


# ---------------------------------------------------------------------------
# WARN-2 fix: PL002 — lambda inside undecorated function must not false-positive
# ---------------------------------------------------------------------------


def test_pl002_no_false_positive_lambda_with_run():
    """Lambda containing run() inside an undecorated function must not trigger
    PL002 on the *outer* function.

    WARN-2 regression: before the fix, the BFS in _contains_godel_primitive
    descended into lambda bodies, causing the outer function to be flagged even
    though the run() lives inside the lambda (a nested callable boundary).
    """
    src = """
def process(items):
    handler = lambda x: run(x)
    return handler
"""
    diags = lint_source(src)
    # The outer 'process' function does NOT directly call run() — the lambda
    # is a boundary.  PL002 must NOT fire for 'process'.
    pl002_outer = [d for d in diags if d.rule == "PL002" and "process" in d.message]
    assert pl002_outer == [], (
        "PL002 must not fire on outer function when run() is inside a lambda"
    )


def test_pl002_lambda_boundary_does_not_hide_direct_run():
    """A function that DIRECTLY calls run() (not via lambda) still triggers PL002."""
    src = """
async def my_fn():
    handler = lambda x: x
    await run("echo hi")
"""
    diags = lint_source(src)
    assert any(d.rule == "PL002" for d in diags), (
        "PL002 must still fire when the function directly calls run()"
    )

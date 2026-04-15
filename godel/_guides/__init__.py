"""Bundled guide markdown files surfaced via ``godel guide``.

Source of truth for human-readable docs is ``docs/``; these files are copies
shipped with the installed package so agents can fetch onboarding content
without the repo being present.

Each entry in :data:`GUIDES` is ``(slug, one-line hook)``.  The slug maps to
``godel/_guides/<slug>.md``.
"""
from __future__ import annotations

GODEL_BLURB = """\
Godel is a deterministic orchestrator for AI agent workflows.  You write
workflows as plain Python — decorated with ``@workflow`` and ``@step`` — and
Godel handles append-only event logging, pause/resume, rewind, and
deterministic replay.  Every step's inputs and outputs are captured in an
audit log so a workflow can be suspended, inspected, repaired, and resumed
without rerunning the expensive parts.
"""

GUIDES: list[tuple[str, str]] = [
    ("getting-started", "Install, first workflow, first run"),
    ("concepts", "Mental model — @workflow, @step, event log, replay"),
    ("engineer", "Author workflows: when to use @step, schemas, failure handling"),
    ("runner",   "Execute / resume / pause / rewind / repair workflows"),
    ("monitoring", "Tail/monitor a live run efficiently without burning context"),
    ("cli",      "Full CLI reference for every godel command"),
    ("api-reference", "Python API reference — decorators, primitives, types"),
]

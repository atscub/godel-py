"""Redaction infrastructure for transcript events.

Redactors are plain callables with the signature::

    (payload: str) -> str | None

*payload* is the JSON-serialised string of the event body (everything inside
the ``{"event": ...}`` wrapper, already encoded as a compact JSON string).

* Return a (possibly modified) ``str`` to keep the event.  The returned string
  replaces the serialised payload verbatim — the writer will wrap it in
  ``{"event": <payload>}`` before appending it to the transcript.
* Return ``None`` to drop the event entirely — nothing is written to disk.
* Raise any exception to trigger the error-substitution path (see below).

This contract mirrors the arity constraint already validated at decoration time
in ``godel/_decorators.py``:  ``Callable[[str], str]``.  The validator
enforces exactly one required positional argument; the ``None`` return for
event-dropping is an allowed extension that falls within Python's type system
(str functions may return None at runtime; the contract is soft).

Error-substitution contract
---------------------------
If a redactor raises ``BaseException`` (including ``KeyboardInterrupt``, etc.)
the failing event is DROPPED and a minimal sentinel event is written instead::

    {"event": {"ts": "<iso>", "seq": <n>, "op": "redactor.error",
               "step_path": [], "stream_path": [],
               "redactor": "<name>", "error_class": "<ExcType>"}}

The sentinel intentionally carries ONLY the redactor name and the exception
*class* name — never the exception message and never any part of the original
event payload.  This prevents secret-leakage through exception reprs.

Subsequent redactors in the pipeline are NOT run after an error on a given
event; the sentinel is the only output for that event.

Zero built-in patterns
-----------------------
``RedactorRegistry()`` with no arguments produces an empty list of redactors.
Godel does not guess at what is a secret.
"""
from __future__ import annotations

import json
from typing import Callable


# Public type alias — a redactor is any callable that takes a serialised event
# string and returns either a string (possibly modified) or None (drop event).
Redactor = Callable[[str], "str | None"]


class RedactorRegistry:
    """Ordered registry of event-redactor callables.

    Redactors are applied in registration order: the output string of redactor
    *N* becomes the input to redactor *N+1*.  The first ``None`` return
    terminates the chain and drops the event.

    Parameters
    ----------
    redactors:
        Zero or more callables conforming to the ``Redactor`` signature.
        Pass nothing (or an empty iterable) to create a no-op registry —
        Godel ships zero built-in patterns.
    """

    def __init__(self, redactors: list[Redactor] | None = None) -> None:
        self._redactors: list[Redactor] = list(redactors) if redactors else []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self, serialised: str, *, sentinel_extras: dict | None = None) -> "str | None":
        """Run *serialised* through all registered redactors in order.

        Parameters
        ----------
        serialised:
            The compact-JSON string of the event body (i.e. the value that will
            be placed inside ``{"event": <serialised>}``).  This is what
            redactors receive and what they must return.
        sentinel_extras:
            Optional dict of extra fields to merge into the error-sentinel event
            body when a redactor raises.  Callers use this to inject ``ts``,
            ``seq``, ``step_path``, and ``stream_path`` so the sentinel is a
            well-formed transcript event.  If ``None``, the sentinel carries
            only ``op``, ``redactor``, and ``error_class``.

        Returns
        -------
        str | None
            * A string (possibly different from input) → write this verbatim as
              the event payload inside ``{"event": ...}``.
            * ``None`` → drop the event silently; the caller writes nothing.

        Notes
        -----
        If a redactor raises ``BaseException``:
        * The original event is treated as dropped.
        * A minimal error-sentinel JSON string is returned (non-None) — callers
          MUST write the returned value as-is.  The sentinel has ``op``:
          ``"redactor.error"``, ``redactor`` (the callable's ``__name__`` or
          ``repr(type(callable))``), and ``error_class`` (the exception type
          name).  No exception message, no original payload.
        * Subsequent redactors are NOT run after an error.
        """
        if not self._redactors:
            return serialised

        current: str | None = serialised
        for redactor in self._redactors:
            if current is None:
                return None
            try:
                current = redactor(current)
            except BaseException as exc:  # noqa: BLE001 — intentional broad catch
                # Determine a safe name for the redactor — never repr the
                # exception or include the original payload in the sentinel.
                name = getattr(redactor, "__name__", None) or repr(type(redactor))
                sentinel: dict = {
                    "op": "redactor.error",
                    "redactor": name,
                    "error_class": type(exc).__name__,
                }
                if sentinel_extras:
                    # Merge caller-supplied context (ts, seq, step_path, etc.)
                    # but never let extras override the three sentinel keys.
                    for k, v in sentinel_extras.items():
                        if k not in sentinel:
                            sentinel[k] = v
                return json.dumps(sentinel, separators=(",", ":"))
        return current

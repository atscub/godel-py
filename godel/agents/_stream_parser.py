"""Tolerant streaming JSONL parser shared by all godel agent adapters.

Reads from any binary IO (subprocess stdout pipe, file, BytesIO) and yields
a sequence of :class:`Parsed` or :class:`Raw` items — one per newline-delimited
line.  The parser never raises: every kind of failure (malformed JSON, oversized
line, decode error, unexpected internal error) surfaces as a :class:`Raw` item
with an explanatory ``reason`` tag.

Vendor-specific classification of *what* a parsed dict means lives in the
individual agent adapters (``_claude.py``, ``_copilot.py``).  This module is
intentionally schema-agnostic.

Design constraints
------------------
* Read in 64 KB chunks to stay memory-efficient on long-running streams.
* Lines longer than 1 MB are truncated to 64 KB and emitted as
  ``Raw(reason="oversized", _truncated=True)``.
* UTF-8 decode with ``errors="replace"`` so stray binary output never crashes.
* CRLF (``\\r\\n``) is handled by stripping ``\\r`` before JSON parsing.
* Chunk-boundary safety: the event sequence is identical regardless of whether
  the underlying stream delivers data in 1-byte or 1 MB chunks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import IO, Iterator, Union

# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

_64KB = 64 * 1024
_1MB = 1024 * 1024
_CHUNK = _64KB


@dataclass
class Parsed:
    """A successfully parsed JSONL line whose value is a JSON object (dict)."""

    data: dict


@dataclass
class Raw:
    """A line that could not be interpreted as a JSON object.

    Attributes
    ----------
    text:
        The (possibly truncated) line content, UTF-8 decoded with replacement.
    reason:
        * ``"malformed"``  — ``json.loads`` raised an error, or a decode error
          occurred so severe the line could not be processed.
        * ``"non_object"`` — valid JSON but the top-level value is not a dict.
        * ``"oversized"``  — line exceeded 1 MB; payload truncated to 64 KB.
        * ``"internal"``   — unexpected exception inside the parser itself.
    _truncated:
        ``True`` when the stored ``text`` is shorter than the original line.
    """

    text: str
    reason: str
    _truncated: bool = field(default=False)


ParseResult = Union[Parsed, Raw]

# ---------------------------------------------------------------------------
# Core iterator
# ---------------------------------------------------------------------------


def iter_parsed(reader: IO[bytes]) -> Iterator[ParseResult]:
    """Yield :class:`Parsed` or :class:`Raw` for every line in *reader*.

    Parameters
    ----------
    reader:
        Any binary-mode IO object that supports ``.read(n)``.  Iteration stops
        when ``.read()`` returns an empty bytes object (EOF).

    Yields
    ------
    :class:`Parsed` or :class:`Raw`
        One item per newline-terminated line (including the final line if it
        lacks a trailing newline).
    """
    # Raw bytes buffer — we accumulate until we see b'\n'.
    buf: bytes = b""

    try:
        while True:
            chunk = reader.read(_CHUNK)
            if not chunk:
                # EOF — flush any remaining buffered data as a final line.
                if buf:
                    yield from _process_line(buf)
                break

            buf += chunk

            # Split on every newline we can find.
            while True:
                nl_pos = buf.find(b"\n")
                if nl_pos == -1:
                    # No complete line yet; check oversized accumulation.
                    if len(buf) > _1MB:
                        # Emit an oversized Raw and *discard* the rest until
                        # we find the next newline on a future chunk.
                        yield _oversized_raw(buf[:_64KB])
                        # Drain the oversized portion — keep scanning for \n.
                        buf = buf[_1MB:]
                    break

                line_bytes = buf[:nl_pos]
                buf = buf[nl_pos + 1 :]  # consume the '\n'

                if len(line_bytes) > _1MB:
                    yield _oversized_raw(line_bytes[:_64KB])
                else:
                    yield from _process_line(line_bytes)

    except Exception as exc:  # pragma: no cover — belt-and-suspenders
        yield Raw(text=repr(exc), reason="internal")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _oversized_raw(truncated_bytes: bytes) -> Raw:
    """Return a ``Raw(reason="oversized", _truncated=True)`` for a huge line."""
    text = truncated_bytes.decode("utf-8", errors="replace")
    return Raw(text=text, reason="oversized", _truncated=True)


def _process_line(line_bytes: bytes) -> Iterator[ParseResult]:
    """Decode and parse a single line, yielding exactly one result."""
    try:
        # Strip CR so CRLF (\r\n) files work transparently.
        text = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")

        # Skip genuinely empty lines (blank / whitespace-only) silently.
        stripped = text.strip()
        if not stripped:
            return

        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            yield Raw(text=text, reason="malformed")
            return

        if not isinstance(value, dict):
            yield Raw(text=text, reason="non_object")
            return

        yield Parsed(data=value)

    except Exception as exc:  # pragma: no cover — absolute last resort
        yield Raw(text=repr(exc), reason="internal")

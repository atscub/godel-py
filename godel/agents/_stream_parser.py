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
    # When True, we have already emitted an oversized Raw for the current
    # line-in-progress; drop every byte until the next newline, then resume
    # normal parsing.  This guarantees an oversized line produces exactly one
    # Raw(reason="oversized") event and zero "malformed" tail fragments.
    dropping_oversized: bool = False

    try:
        while True:
            chunk = reader.read(_CHUNK)
            if not chunk:
                # EOF — flush any remaining buffered data as a final line.
                if dropping_oversized:
                    # Oversized line had no trailing newline — we already
                    # emitted its Raw; just drop what's left.
                    pass
                elif buf:
                    if len(buf) > _1MB:
                        yield _oversized_raw(buf[:_64KB])
                    else:
                        yield from _process_line(buf)
                break

            if dropping_oversized:
                nl_pos = chunk.find(b"\n")
                if nl_pos == -1:
                    # Still no terminator — discard the whole chunk.
                    continue
                # Found the end of the oversized line; resume normal parsing
                # with the bytes after the newline.
                dropping_oversized = False
                buf = chunk[nl_pos + 1 :]
            else:
                buf += chunk

            # Split on every newline we can find.
            while True:
                nl_pos = buf.find(b"\n")
                if nl_pos == -1:
                    # No complete line yet; check oversized accumulation.
                    if len(buf) > _1MB:
                        # Emit exactly one oversized Raw for this line and
                        # switch to drain mode — every subsequent byte up to
                        # and including the next '\n' is discarded.
                        yield _oversized_raw(buf[:_64KB])
                        buf = b""
                        dropping_oversized = True
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
    """Return a ``Raw(reason="oversized", _truncated=True)`` for a huge line.

    The payload is hard-capped so that the resulting ``text`` encodes to
    <= 64 KB of UTF-8 bytes.  Because ``errors="replace"`` can expand a single
    invalid byte into a 3-byte U+FFFD sequence, we decode then re-trim by
    encoded length.
    """
    # Start with at most 64 KB of input bytes so we never spend unbounded CPU.
    slice_bytes = truncated_bytes[:_64KB]
    text = slice_bytes.decode("utf-8", errors="replace")
    # Trim characters from the tail until the encoded length fits in 64 KB.
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > _64KB:
        # Binary search for the largest prefix whose UTF-8 encoding fits.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(text[:mid].encode("utf-8", errors="replace")) <= _64KB:
                lo = mid
            else:
                hi = mid - 1
        text = text[:lo]
    return Raw(text=text, reason="oversized", _truncated=True)


class StreamingParser:
    """Push-mode parser for incremental byte feeds.

    Unlike :func:`iter_parsed`, which requires a complete binary IO object,
    ``StreamingParser`` accepts arbitrary byte chunks via :meth:`feed` and
    yields :class:`Parsed` / :class:`Raw` items as complete lines become
    available.  Partial lines are buffered internally until the next newline
    arrives or :meth:`close` is called.

    The event sequence produced is identical to :func:`iter_parsed` for the
    same byte stream regardless of how the bytes are chunked — the semantics
    at chunk boundaries are identical.

    Usage::

        parser = StreamingParser()
        for chunk in stream:
            for item in parser.feed(chunk):
                handle(item)
        for item in parser.close():
            handle(item)
    """

    def __init__(self) -> None:
        self._buf: bytes = b""
        self._dropping_oversized: bool = False

    def feed(self, chunk: bytes) -> Iterator[ParseResult]:
        """Push *chunk* into the parser; yield complete-line results."""
        if not chunk:
            return

        if self._dropping_oversized:
            nl_pos = chunk.find(b"\n")
            if nl_pos == -1:
                return  # still draining oversized line
            self._dropping_oversized = False
            self._buf = chunk[nl_pos + 1:]
        else:
            self._buf += chunk

        # Drain all complete lines from the buffer.
        while True:
            nl_pos = self._buf.find(b"\n")
            if nl_pos == -1:
                # No complete line yet — check oversized accumulation.
                if len(self._buf) > _1MB:
                    yield _oversized_raw(self._buf[:_64KB])
                    self._buf = b""
                    self._dropping_oversized = True
                break

            line_bytes = self._buf[:nl_pos]
            self._buf = self._buf[nl_pos + 1:]

            if len(line_bytes) > _1MB:
                yield _oversized_raw(line_bytes[:_64KB])
            else:
                yield from _process_line(line_bytes)

    def close(self) -> Iterator[ParseResult]:
        """Flush any buffered partial line and yield the final result (if any)."""
        if self._dropping_oversized:
            # Oversized line with no trailing newline — already emitted Raw.
            self._buf = b""
            return
        if self._buf:
            remaining = self._buf
            self._buf = b""
            if len(remaining) > _1MB:
                yield _oversized_raw(remaining[:_64KB])
            else:
                yield from _process_line(remaining)


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

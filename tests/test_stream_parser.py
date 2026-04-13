"""Tests for godel.agents._stream_parser — tolerant streaming JSONL parser.

Acceptance criteria checked here:
- Fuzz battery never raises (malformed JSON, CRLF mix, mid-UTF8 split across
  chunk boundary, 10 MB single line, 100k short lines, missing trailing newline).
- Oversized lines yield Raw(reason="oversized") with payload <= 64 KB.
- Chunk-boundary safety: identical event sequence for 1-byte vs 1 MB chunks.
- Malformed → Raw(reason="malformed"); valid non-object JSON → Raw(reason="non_object").
- Sample fixture files are parsed without errors.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import random
import string

import pytest

from godel.agents._stream_parser import Parsed, Raw, _64KB, _1MB, iter_parsed

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "stream_json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_bytes(data: bytes) -> list[Parsed | Raw]:
    """Parse *data* with the default chunk size."""
    return list(iter_parsed(io.BytesIO(data)))


def parse_bytes_chunked(data: bytes, chunk_size: int) -> list[Parsed | Raw]:
    """Parse *data* feeding *chunk_size* bytes at a time."""

    class ChunkedReader:
        def __init__(self, data: bytes, size: int):
            self._data = data
            self._pos = 0
            self._size = size

        def read(self, n: int) -> bytes:
            chunk = self._data[self._pos : self._pos + self._size]
            self._pos += self._size
            return chunk

    return list(iter_parsed(ChunkedReader(data, chunk_size)))


def _results_equal(a: list, b: list) -> bool:
    """Compare two result lists for equality, ignoring text field differences
    only for oversized items (truncation point may land differently by chunk)."""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if type(x) != type(y):
            return False
        if isinstance(x, Parsed):
            if x.data != y.data:
                return False
        else:  # Raw
            if x.reason != y.reason:
                return False
            if x._truncated != y._truncated:
                return False
            # For non-oversized Raw, text should be identical
            if not x._truncated and x.text != y.text:
                return False
    return True


# ---------------------------------------------------------------------------
# Basic happy-path
# ---------------------------------------------------------------------------


def test_single_valid_object():
    data = b'{"key": "value"}\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Parsed)
    assert results[0].data == {"key": "value"}


def test_multiple_valid_objects():
    lines = [{"id": i, "v": "x" * 10} for i in range(5)]
    data = "\n".join(json.dumps(l) for l in lines).encode() + b"\n"
    results = parse_bytes(data)
    assert len(results) == 5
    for r, line in zip(results, lines):
        assert isinstance(r, Parsed)
        assert r.data == line


def test_missing_trailing_newline():
    """Last line without trailing newline must still be yielded."""
    data = b'{"a": 1}\n{"b": 2}'
    results = parse_bytes(data)
    assert len(results) == 2
    assert all(isinstance(r, Parsed) for r in results)
    assert results[0].data == {"a": 1}
    assert results[1].data == {"b": 2}


def test_empty_lines_skipped():
    data = b'{"a":1}\n\n\n{"b":2}\n'
    results = parse_bytes(data)
    assert len(results) == 2


def test_whitespace_only_lines_skipped():
    data = b'{"a":1}\n   \t  \n{"b":2}\n'
    results = parse_bytes(data)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# CRLF handling
# ---------------------------------------------------------------------------


def test_crlf_line_endings():
    data = b'{"x": 1}\r\n{"y": 2}\r\n'
    results = parse_bytes(data)
    assert len(results) == 2
    assert all(isinstance(r, Parsed) for r in results)
    assert results[0].data == {"x": 1}
    assert results[1].data == {"y": 2}


def test_mixed_lf_crlf():
    data = b'{"a":1}\n{"b":2}\r\n{"c":3}\n'
    results = parse_bytes(data)
    assert len(results) == 3
    assert all(isinstance(r, Parsed) for r in results)


def test_crlf_fixture_file():
    path = FIXTURES / "crlf_sample.jsonl"
    with open(path, "rb") as f:
        results = list(iter_parsed(f))
    assert len(results) == 2
    assert all(isinstance(r, Parsed) for r in results)


# ---------------------------------------------------------------------------
# Malformed and non-object
# ---------------------------------------------------------------------------


def test_malformed_json():
    data = b'not json at all\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "malformed"


def test_truncated_json():
    data = b'{"key": \n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "malformed"


def test_non_object_array():
    data = b'[1, 2, 3]\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "non_object"


def test_non_object_string():
    data = b'"hello"\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "non_object"


def test_non_object_number():
    data = b'42\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "non_object"


def test_non_object_null():
    data = b'null\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "non_object"


def test_non_object_bool():
    data = b'true\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "non_object"


def test_mixed_valid_and_malformed():
    data = b'{"ok": 1}\nnot json\n{"also":"ok"}\n[1,2]\n'
    results = parse_bytes(data)
    assert len(results) == 4
    assert isinstance(results[0], Parsed)
    assert isinstance(results[1], Raw) and results[1].reason == "malformed"
    assert isinstance(results[2], Parsed)
    assert isinstance(results[3], Raw) and results[3].reason == "non_object"


# ---------------------------------------------------------------------------
# Non-UTF-8 handling
# ---------------------------------------------------------------------------


def test_non_utf8_bytes_replaced():
    """Non-UTF-8 bytes in a line should produce Raw(reason="malformed") after
    replacement — the parser should NOT raise."""
    # b'\xff\xfe' is not valid UTF-8; the line won't be valid JSON either.
    data = b'\xff\xfe{"key": "val"}\n'
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    # After replacement it won't parse as JSON
    assert results[0].reason in ("malformed", "non_object")


def test_non_utf8_inside_json_value():
    """Bytes that survive replacement and still form valid JSON should parse."""
    # Build a JSON object where one byte sequence gets replaced but the
    # surrounding structure stays valid JSON.
    raw_json = b'{"note": "caf\xe9"}\n'
    results = parse_bytes(raw_json)
    # After UTF-8 replacement, 'caf\ufffd' is still a valid JSON string value
    assert len(results) == 1
    assert isinstance(results[0], Parsed)
    assert "note" in results[0].data


def test_mid_utf8_split_across_chunk_boundary():
    """A multi-byte UTF-8 codepoint split across a chunk boundary must not crash.

    U+00E9 (é) encodes as 0xc3 0xa9 in UTF-8.  We feed the first byte in one
    chunk and the second in the next.  The line itself may not parse as JSON,
    but the parser must not raise.
    """
    # Construct raw bytes: valid JSON wrapping a string with é split across chunks
    inner = b'{"k":"caf\xc3\xa9"}\n'
    results = parse_bytes_chunked(inner, chunk_size=1)
    # Must not raise; result count is 1
    assert len(results) == 1
    assert isinstance(results[0], (Parsed, Raw))


# ---------------------------------------------------------------------------
# Oversized lines
# ---------------------------------------------------------------------------


def test_oversized_line_yields_raw():
    """A line longer than 1 MB must yield Raw(reason='oversized')."""
    big_line = b"x" * (_1MB + 100) + b"\n"
    results = parse_bytes(big_line)
    assert len(results) == 1
    assert isinstance(results[0], Raw)
    assert results[0].reason == "oversized"
    assert results[0]._truncated is True
    assert len(results[0].text.encode()) <= _64KB + 10  # allow small decode overhead


def test_oversized_line_payload_le_64kb():
    """The text stored in an oversized Raw must not exceed 64 KB."""
    big_line = b"y" * (5 * _1MB) + b"\n"
    results = parse_bytes(big_line)
    oversized = [r for r in results if isinstance(r, Raw) and r.reason == "oversized"]
    assert len(oversized) >= 1
    for r in oversized:
        assert len(r.text.encode("utf-8")) <= _64KB + 10


def test_10mb_single_line():
    """10 MB single line — the fuzz battery criterion."""
    big_line = b"z" * (10 * _1MB)  # no trailing newline
    results = parse_bytes(big_line)
    assert len(results) >= 1
    for r in results:
        assert isinstance(r, Raw)
        if r._truncated:
            assert len(r.text.encode()) <= _64KB + 10


def test_normal_lines_after_oversized():
    """Oversized line must not corrupt subsequent lines."""
    big = b"B" * (_1MB + 1) + b"\n"
    normal = b'{"after": true}\n'
    data = big + normal
    results = parse_bytes(data)
    parsed_items = [r for r in results if isinstance(r, Parsed)]
    assert any(p.data == {"after": True} for p in parsed_items)


# ---------------------------------------------------------------------------
# Chunk-boundary safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 1024, _64KB, _1MB])
def test_chunk_boundary_consistency(chunk_size):
    """Same event sequence regardless of chunk size."""
    lines = [{"id": i, "data": "hello world " * 5} for i in range(20)]
    data = "\n".join(json.dumps(l) for l in lines).encode() + b"\n"

    full_results = parse_bytes(data)
    chunked_results = parse_bytes_chunked(data, chunk_size)

    assert _results_equal(full_results, chunked_results), (
        f"chunk_size={chunk_size}: got {len(chunked_results)} items "
        f"vs {len(full_results)} for default"
    )


def test_chunk_boundary_mid_json():
    """A JSON object split right across a chunk boundary must parse correctly."""
    obj = json.dumps({"hello": "world", "num": 42}).encode() + b"\n"
    # Feed 1 byte at a time — hardest possible split.
    results = parse_bytes_chunked(obj, chunk_size=1)
    assert len(results) == 1
    assert isinstance(results[0], Parsed)
    assert results[0].data == {"hello": "world", "num": 42}


# ---------------------------------------------------------------------------
# Scale / fuzz battery
# ---------------------------------------------------------------------------


def test_100k_short_lines():
    """100 000 short lines must all parse correctly."""
    lines = [json.dumps({"i": i}) for i in range(100_000)]
    data = "\n".join(lines).encode() + b"\n"
    results = parse_bytes(data)
    assert len(results) == 100_000
    assert all(isinstance(r, Parsed) for r in results)


def test_fuzz_random_bytes_never_raises():
    """Random bytes should never cause an exception — only Raw items."""
    rng = random.Random(0xDEADBEEF)
    for _ in range(50):
        size = rng.randint(0, 8192)
        data = bytes(rng.randint(0, 255) for _ in range(size))
        try:
            results = list(iter_parsed(io.BytesIO(data)))
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"iter_parsed raised: {exc!r}")
        # All items must be Parsed or Raw
        for r in results:
            assert isinstance(r, (Parsed, Raw))


def test_fuzz_malformed_json_mix_never_raises():
    """Mixed valid/invalid JSON lines never raise."""
    fragments = [
        b'{"ok": 1}\n',
        b'not json\n',
        b'{"nested": {"a": [1,2,3]}}\n',
        b'[1, 2]\n',
        b'\x80\x81\x82\n',
        b'"just a string"\n',
        b'null\n',
        b'{\n',  # truncated
        b'}\n',  # invalid
        b'{"emoji": "\xf0\x9f\x98\x80"}\n',
    ]
    data = b"".join(fragments * 10)
    try:
        results = list(iter_parsed(io.BytesIO(data)))
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"iter_parsed raised: {exc!r}")
    assert len(results) > 0


def test_fuzz_crlf_mix_never_raises():
    """CRLF/LF mix never raises."""
    lines = []
    for i in range(200):
        ending = b"\r\n" if i % 2 == 0 else b"\n"
        if i % 5 == 0:
            lines.append(b"not json" + ending)
        else:
            lines.append(json.dumps({"i": i}).encode() + ending)
    data = b"".join(lines)
    try:
        results = list(iter_parsed(io.BytesIO(data)))
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"iter_parsed raised: {exc!r}")
    assert len(results) == 200


# ---------------------------------------------------------------------------
# Fixture file smoke tests
# ---------------------------------------------------------------------------


def test_claude_sample_fixture():
    path = FIXTURES / "claude_sample.jsonl"
    with open(path, "rb") as f:
        results = list(iter_parsed(f))
    assert len(results) == 3
    assert all(isinstance(r, Parsed) for r in results)


def test_copilot_sample_fixture():
    path = FIXTURES / "copilot_sample.jsonl"
    with open(path, "rb") as f:
        results = list(iter_parsed(f))
    assert len(results) == 2
    assert all(isinstance(r, Parsed) for r in results)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input():
    results = parse_bytes(b"")
    assert results == []


def test_only_newlines():
    results = parse_bytes(b"\n\n\n")
    assert results == []


def test_deeply_nested_json():
    obj: dict = {}
    cur = obj
    for i in range(50):
        cur["child"] = {}
        cur = cur["child"]
    cur["leaf"] = "value"
    data = json.dumps(obj).encode() + b"\n"
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Parsed)


def test_large_json_object_under_1mb():
    """A large (but sub-1MB) JSON object should parse as Parsed."""
    obj = {f"key_{i}": "v" * 100 for i in range(500)}
    data = json.dumps(obj).encode() + b"\n"
    assert len(data) < _1MB
    results = parse_bytes(data)
    assert len(results) == 1
    assert isinstance(results[0], Parsed)


def test_raw_text_preserved_for_malformed():
    """The text in Raw(reason='malformed') should contain the original line."""
    line = b"this is not json\n"
    results = parse_bytes(line)
    assert isinstance(results[0], Raw)
    assert "this is not json" in results[0].text


def test_raw_text_preserved_for_non_object():
    """The text in Raw(reason='non_object') should contain the original line."""
    line = b'"a plain string"\n'
    results = parse_bytes(line)
    assert isinstance(results[0], Raw)
    assert "plain string" in results[0].text

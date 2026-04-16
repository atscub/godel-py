"""Tests for AST pre-scan (Layer 1 strict mode)."""
from godel._strict_ast import scan_source, scan_file, BANNED_MODULES, BANNED_ATTR_CALLS


def test_detects_import_requests():
    vs = scan_source("import requests")
    assert len(vs) == 1
    assert "requests" in vs[0].message
    assert vs[0].layer == "ast"


def test_detects_from_import_socket():
    vs = scan_source("from socket import socket")
    assert len(vs) == 1
    assert "socket" in vs[0].message


def test_detects_time_time():
    vs = scan_source("import time\ntime.time()")
    assert len(vs) == 1
    assert "time.time()" in vs[0].message


def test_detects_datetime_now():
    vs = scan_source("from datetime import datetime\ndatetime.now()")
    assert len(vs) == 1
    assert "datetime.now()" in vs[0].message


def test_detects_random_random():
    vs = scan_source("import random\nrandom.random()")
    assert len(vs) == 1
    assert "random.random()" in vs[0].message


def test_detects_uuid_uuid4():
    vs = scan_source("import uuid\nuuid.uuid4()")
    assert len(vs) == 1
    assert "uuid.uuid4()" in vs[0].message


def test_detects_aliased_import():
    vs = scan_source("import time as t\nt.time()")
    assert len(vs) == 1
    assert "time.time()" in vs[0].message


def test_clean_file():
    vs = scan_source("import json\nx = json.dumps({'a': 1})")
    assert len(vs) == 0


def test_godel_det_not_flagged():
    vs = scan_source("from godel import det\ndet.now()")
    assert len(vs) == 0


def test_multiple_violations():
    code = "import requests\nimport time\ntime.time()\n"
    vs = scan_source(code)
    assert len(vs) == 2


def test_line_col_numbers():
    vs = scan_source("x = 1\nimport requests")
    assert len(vs) == 1
    assert vs[0].line == 2


def test_scan_file(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("import socket\n")
    vs = scan_file(str(f), raise_on_violation=False)
    assert len(vs) == 1


def test_scan_file_raises(tmp_path):
    import pytest
    from godel._exceptions import GodelStrictError
    f = tmp_path / "test.py"
    f.write_text("import requests\n")
    with pytest.raises(GodelStrictError) as exc_info:
        scan_file(str(f))
    assert len(exc_info.value.violations) == 1


def test_detects_asyncio_sleep():
    vs = scan_source("import asyncio\nawait asyncio.sleep(1)")
    assert len(vs) == 1
    assert "asyncio.sleep()" in vs[0].message
    assert "godel.sleep" in vs[0].message


def test_detects_time_sleep_hint():
    """time.sleep should also suggest godel.sleep."""
    vs = scan_source("import time\ntime.sleep(1)")
    assert len(vs) == 1
    assert "godel.sleep" in vs[0].message


def test_asyncio_sleep_aliased():
    vs = scan_source("import asyncio as aio\nawait aio.sleep(1)")
    assert len(vs) == 1
    assert "asyncio.sleep()" in vs[0].message


def test_from_asyncio_import_sleep_bare_call():
    """`from asyncio import sleep; sleep(1)` must still be flagged (W1)."""
    vs = scan_source("from asyncio import sleep\nawait sleep(1)")
    assert len(vs) == 1
    assert "asyncio.sleep()" in vs[0].message
    assert "godel.sleep" in vs[0].message


def test_from_time_import_sleep_bare_call():
    """`from time import sleep; sleep(1)` must still be flagged (W1)."""
    vs = scan_source("from time import sleep\nsleep(1)")
    assert len(vs) == 1
    assert "time.sleep()" in vs[0].message
    assert "godel.sleep" in vs[0].message


def test_from_random_import_random_bare_call():
    """Generalised: non-sleep banned bare-Name calls also flagged."""
    vs = scan_source("from random import random\nrandom()")
    assert len(vs) == 1
    assert "random.random()" in vs[0].message
    assert "godel.det" in vs[0].message


def test_bare_name_unrelated_calls_not_flagged():
    """Bare calls to non-banned names must not be false-flagged."""
    vs = scan_source("from json import dumps\ndumps({'a': 1})")
    assert len(vs) == 0

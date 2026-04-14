import re
import godel


def test_version_returns_semver_string():
    v = godel.version()
    assert isinstance(v, str)
    assert re.match(r"^\d+\.\d+\.\d+", v), f"Expected semver-like string, got {v!r}"

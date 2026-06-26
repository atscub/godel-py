"""Tests for strict mode and det exports."""


def test_strict_exports():
    from godel import GodelStrictError, det
    assert issubclass(GodelStrictError, Exception)
    assert callable(det.now)
    assert callable(det.random)
    assert callable(det.uuid4)


def test_det_module_accessible():
    import godel
    assert hasattr(godel, "det")
    assert hasattr(godel.det, "now")
    assert hasattr(godel.det, "random")
    assert hasattr(godel.det, "uuid4")

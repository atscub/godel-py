"""Tests for the ``.godel/`` + ``~/.godel/`` two-tier config loader."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from godel._config import (
    CONFIG_DIR_NAME,
    SETTINGS_FILENAME,
    GodelConfig,
    clear_cache,
    find_project_config,
    list_workflows,
    load_config,
    project_data_dir,
    resolve_workflow,
)
from godel._exceptions import ConfigError


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isolate every test from the real ``~/.godel`` and any ambient GODEL_*."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GODEL_HOME", str(fake_home / ".godel"))
    for k in list(os.environ):
        if k.startswith("GODEL_") and k not in ("GODEL_HOME",):
            monkeypatch.delenv(k, raising=False)
    clear_cache()
    yield
    clear_cache()


def _write_project(root: Path, data: dict) -> Path:
    """Create ``<root>/.godel/settings.json`` with *data*."""
    d = root / CONFIG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    (d / SETTINGS_FILENAME).write_text(json.dumps(data))
    return d


def _write_user(data: dict) -> Path:
    """Create ``$GODEL_HOME/settings.json`` with *data*."""
    d = Path(os.environ["GODEL_HOME"])
    d.mkdir(parents=True, exist_ok=True)
    (d / SETTINGS_FILENAME).write_text(json.dumps(data))
    return d / SETTINGS_FILENAME


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_defaults_only(tmp_path):
    cfg = load_config(tmp_path).config
    assert isinstance(cfg, GodelConfig)
    assert cfg.strict is True
    assert cfg.runs_dir is None


def test_user_layer(tmp_path):
    _write_user({"strict": False})
    cfg = load_config(tmp_path).config
    assert cfg.strict is False


def test_project_overrides_user(tmp_path):
    _write_user({"strict": False, "lint": False})
    _write_project(tmp_path, {"strict": True})
    cfg = load_config(tmp_path).config
    assert cfg.strict is True    # project wins
    assert cfg.lint is False      # user persists where project silent


def test_env_overrides_project(tmp_path, monkeypatch):
    _write_project(tmp_path, {"strict": True})
    monkeypatch.setenv("GODEL_STRICT", "0")
    clear_cache()
    cfg = load_config(tmp_path).config
    assert cfg.strict is False


def test_env_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GODEL_RUNS_DIR", "/tmp/mine")
    clear_cache()
    cfg = load_config(tmp_path).config
    assert cfg.runs_dir == "/tmp/mine"


def test_missing_user_layer_skipped(tmp_path):
    # No user settings.json on disk — should not error.
    cfg = load_config(tmp_path).config
    assert cfg.strict is True


def test_malformed_json_raises(tmp_path):
    d = tmp_path / CONFIG_DIR_NAME
    d.mkdir()
    (d / SETTINGS_FILENAME).write_text("{ not json")
    with pytest.raises(ConfigError, match="malformed JSON"):
        load_config(tmp_path)


def test_unknown_field_rejected(tmp_path):
    _write_project(tmp_path, {"nonsense": 1})
    with pytest.raises(ConfigError):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Walk-up discovery
# ---------------------------------------------------------------------------

def test_walkup_finds_config_in_parent(tmp_path):
    _write_project(tmp_path, {})
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_project_config(deep) == tmp_path / CONFIG_DIR_NAME


def test_walkup_stops_at_git(tmp_path):
    _write_project(tmp_path, {})                   # .godel at tmp_path
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").mkdir()                         # .git below config
    # From inside `sub/`, walk-up must stop at `sub` (which has .git) before
    # reaching tmp_path — so no .godel is found.
    deep = sub / "deeper"
    deep.mkdir()
    assert find_project_config(deep) is None


def test_walkup_stops_at_home(tmp_path, monkeypatch):
    # Place a .godel/ ABOVE $HOME; walk-up from inside $HOME must NOT find it.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / CONFIG_DIR_NAME).mkdir()  # .godel outside $HOME — should be invisible
    sub = home / "proj" / "deeper"
    sub.mkdir(parents=True)
    assert find_project_config(sub) is None


def test_walkup_returns_none_at_root(tmp_path):
    # Pure tmp dir, no .godel anywhere — walk-up eventually hits fs root.
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    # Result depends on real fs; we only assert it returns cleanly.
    assert find_project_config(deep) is None


# ---------------------------------------------------------------------------
# Per-project data dir
# ---------------------------------------------------------------------------

def test_project_data_dir_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = home / "work" / "repo"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GODEL_HOME", str(home / ".godel"))
    p = project_data_dir(proj)
    assert p == (home / ".godel" / "projects" / "work" / "repo").resolve()


def test_project_data_dir_outside_home_uses_abs_bucket(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GODEL_HOME", str(home / ".godel"))
    outside = tmp_path / "outside"
    outside.mkdir()
    p = project_data_dir(outside)
    assert "_abs" in p.parts
    # Deterministic: same input -> same bucket.
    assert project_data_dir(outside) == p


# ---------------------------------------------------------------------------
# runs_dir resolution via LoadedConfig
# ---------------------------------------------------------------------------

def test_runs_dir_defaults_to_project_data(tmp_path):
    _write_project(tmp_path, {})
    loaded = load_config(tmp_path)
    assert loaded.project_root == tmp_path.resolve()
    # runs_dir should live under the fake $GODEL_HOME, not inside the project.
    assert str(loaded.runs_dir).startswith(os.environ["GODEL_HOME"])
    assert loaded.runs_dir.name == "runs"


def test_runs_dir_explicit_absolute(tmp_path):
    _write_project(tmp_path, {"runs_dir": str(tmp_path / "custom")})
    loaded = load_config(tmp_path)
    assert loaded.runs_dir == tmp_path / "custom"


def test_runs_dir_explicit_relative_resolves_against_project(tmp_path):
    _write_project(tmp_path, {"runs_dir": "myruns"})
    loaded = load_config(tmp_path)
    assert loaded.runs_dir == (tmp_path / "myruns").resolve()


# ---------------------------------------------------------------------------
# Workflow name resolution
# ---------------------------------------------------------------------------

def test_resolve_workflow_by_path_wins(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("# hello")
    loaded = load_config(tmp_path)
    assert resolve_workflow(str(f), loaded) == f.resolve()


def test_resolve_workflow_by_name_project(tmp_path):
    _write_project(tmp_path, {})
    wfdir = tmp_path / CONFIG_DIR_NAME / "workflows"
    wfdir.mkdir(parents=True)
    f = wfdir / "foo.py"
    f.write_text("# foo")
    loaded = load_config(tmp_path)
    assert resolve_workflow("foo", loaded) == f.resolve()


def test_resolve_workflow_project_shadows_user(tmp_path):
    _write_project(tmp_path, {})
    proj_wf = tmp_path / CONFIG_DIR_NAME / "workflows"
    proj_wf.mkdir(parents=True)
    proj_file = proj_wf / "shared.py"
    proj_file.write_text("# project")

    user_wf = Path(os.environ["GODEL_HOME"]) / "workflows"
    user_wf.mkdir(parents=True)
    (user_wf / "shared.py").write_text("# user")

    loaded = load_config(tmp_path)
    assert resolve_workflow("shared", loaded) == proj_file.resolve()


def test_resolve_workflow_missing_raises(tmp_path):
    _write_project(tmp_path, {})
    loaded = load_config(tmp_path)
    with pytest.raises(ConfigError, match="workflow not found"):
        resolve_workflow("nope", loaded)


def test_list_workflows_merges_project_and_user(tmp_path):
    _write_project(tmp_path, {})
    proj_wf = tmp_path / CONFIG_DIR_NAME / "workflows"
    proj_wf.mkdir(parents=True)
    (proj_wf / "a.py").write_text("")
    user_wf = Path(os.environ["GODEL_HOME"]) / "workflows"
    user_wf.mkdir(parents=True)
    (user_wf / "b.py").write_text("")
    loaded = load_config(tmp_path)
    found = list_workflows(loaded)
    assert set(found) == {"a", "b"}


# ---------------------------------------------------------------------------
# Memoization
# ---------------------------------------------------------------------------

def test_load_config_memoized_same_inputs(tmp_path):
    _write_project(tmp_path, {})
    first = load_config(tmp_path)
    second = load_config(tmp_path)
    assert first is second


def test_load_config_clear_cache(tmp_path):
    _write_project(tmp_path, {})
    first = load_config(tmp_path)
    clear_cache()
    second = load_config(tmp_path)
    assert first is not second


def test_symlinked_cwd_resolves(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _write_project(real, {"strict": False})
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = load_config(link).config
    assert cfg.strict is False


def test_empty_env_var_does_not_override(tmp_path, monkeypatch):
    _write_project(tmp_path, {"workflows_dir": "custom/wfs"})
    monkeypatch.setenv("GODEL_WORKFLOWS_DIR", "")  # empty must not override
    clear_cache()
    cfg = load_config(tmp_path).config
    assert cfg.workflows_dir == "custom/wfs"


def test_open_event_log_uses_configured_runs_dir(tmp_path, monkeypatch):
    from godel._event_log import EventLog
    from godel import open_event_log

    runs = tmp_path / "myruns"
    runs.mkdir()
    monkeypatch.setenv("GODEL_RUNS_DIR", str(runs))
    clear_cache()
    # Create a log via EventLog directly, then re-open via the public helper.
    EventLog("r-1", runs_dir=str(runs))
    reopened = open_event_log("r-1", cwd=tmp_path)
    assert reopened._run_id == "r-1"
    assert str(reopened._file_path).startswith(str(runs))


def test_init_idempotent(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from godel.cli import main
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    first = r.invoke(main, ["init"])
    assert first.exit_code == 0
    assert "created" in first.output
    second = r.invoke(main, ["init"])
    assert second.exit_code == 0
    assert "created" not in second.output
    assert "exists, skipped" in second.output


def test_load_config_env_change_invalidates(tmp_path, monkeypatch):
    _write_project(tmp_path, {})
    first = load_config(tmp_path).config
    monkeypatch.setenv("GODEL_STRICT", "0")
    second = load_config(tmp_path).config
    assert first.strict is True and second.strict is False

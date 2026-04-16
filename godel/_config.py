"""Two-tier config for godel.

Project-level: ``<project>/.godel/settings.json`` (committed).
User-level:    ``~/.godel/settings.json`` (+ per-project data under
               ``~/.godel/projects/<rel>/``).

Precedence (high→low): CLI flag > env var > project > user > default.

See ``plans/steady-sniffing-candy.md`` for design notes.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from godel._exceptions import ConfigError


CONFIG_DIR_NAME = ".godel"
SETTINGS_FILENAME = "settings.json"
WORKFLOWS_SUBDIR = "workflows"


class WatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    default: bool = False
    plain: bool = False


class GodelConfig(BaseModel):
    """Frozen merged config.  All fields optional with sensible defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_dir: str | None = None
    workflows_dir: str = f"{CONFIG_DIR_NAME}/{WORKFLOWS_SUBDIR}"
    strict: bool = True
    lint: bool = True
    stream_agents: bool = True
    watch: WatchConfig = Field(default_factory=WatchConfig)
    env: dict[str, str] = Field(default_factory=dict)
    redact: list[str] = Field(default_factory=list)
    capture_stdout: bool = False
    transcript_max_bytes: int = 10 * 1024 * 1024
    # NOTE: auto-checkpoint mode is a GODEL_AUTO_CHECKPOINT env var / CLI flag
    # only — intentionally not a GodelConfig field.  It's execution-context
    # metadata (how stdin answers are supplied for THIS invocation), not a
    # persistable project setting.  godel.io reads os.environ directly.


@dataclass(frozen=True)
class LoadedConfig:
    """Result of ``load_config``: merged model + provenance."""
    config: GodelConfig
    project_root: Path | None
    sources: list[Path] = field(default_factory=list)

    @property
    def runs_dir(self) -> Path:
        """Resolved absolute runs directory.

        If ``config.runs_dir`` is unset, defaults to
        ``~/.godel/projects/<rel>/runs`` (or ``./runs`` when no project root
        is known — zero-config programmatic use).
        """
        if self.config.runs_dir:
            rd = Path(self.config.runs_dir).expanduser()
            if rd.is_absolute():
                return rd
            base = self.project_root if self.project_root else Path.cwd()
            return (base / rd).resolve()
        if self.project_root is None:
            return (Path.cwd() / "runs").resolve()
        return project_data_dir(self.project_root) / "runs"


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def global_config_dir() -> Path:
    """``~/.godel/`` (or ``$GODEL_HOME`` if set)."""
    override = os.environ.get("GODEL_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / CONFIG_DIR_NAME).resolve()


def project_data_dir(project_root: Path) -> Path:
    """``~/.godel/projects/<rel>/`` for this project.

    ``<rel>`` = project_root with ``$HOME/`` stripped.  For projects outside
    ``$HOME``, use ``_abs/<sha256(realpath)[:16]>``.
    """
    project_root = Path(project_root).resolve()
    home = Path.home().resolve()
    try:
        rel = project_root.relative_to(home)
        return global_config_dir() / "projects" / rel
    except ValueError:
        digest = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]
        return global_config_dir() / "projects" / "_abs" / digest


def find_project_config(cwd: Path | None = None) -> Path | None:
    """Walk up from *cwd* looking for a ``.godel/`` directory.

    Stops at first of: filesystem root, ``$HOME``, or a dir containing ``.git/``
    (that dir is still checked for ``.godel/`` before stopping).  Returns the
    path of the ``.godel/`` dir, or ``None`` if none found.
    """
    start = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    home = Path.home().resolve()

    current = start
    while True:
        candidate = current / CONFIG_DIR_NAME
        if candidate.is_dir():
            return candidate

        # Stop conditions (check *after* examining current dir):
        if current == current.parent:
            return None  # filesystem root
        if current == home:
            return None
        if (current / ".git").exists():
            return None

        current = current.parent


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_BOOL_ENV_KEYS = {
    "GODEL_STRICT": "strict",
    "GODEL_LINT": "lint",
    "GODEL_STREAM_AGENTS": "stream_agents",
    "GODEL_CAPTURE_STDOUT": "capture_stdout",
}

_STR_ENV_KEYS = {
    "GODEL_RUNS_DIR": "runs_dir",
    "GODEL_WORKFLOWS_DIR": "workflows_dir",
}


def _read_json(path: Path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level JSON must be an object")
    return data


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "")


def _env_overrides() -> dict:
    out: dict = {}
    for env_key, field_name in _STR_ENV_KEYS.items():
        v = os.environ.get(env_key)
        if v:  # ignore unset and empty-string
            out[field_name] = v
    for env_key, field_name in _BOOL_ENV_KEYS.items():
        v = os.environ.get(env_key)
        if v is not None and v != "":
            out[field_name] = _parse_bool(v)
    return out


def _merge(base: dict, overlay: dict) -> dict:
    """Shallow merge; nested dicts (watch, env) shallow-merged too."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


_cache: dict[tuple, "LoadedConfig"] = {}


def load_config(cwd: Path | str | None = None, *, use_cache: bool = True) -> LoadedConfig:
    """Load merged config.

    Layers (low→high): built-in defaults < ``~/.godel/settings.json`` <
    ``<project>/.godel/settings.json`` < ``GODEL_*`` env vars.  CLI flags are
    applied by callers on top of the returned model.

    Memoized on ``(realpath(cwd), frozen env snapshot)`` — deep/NFS paths hit
    the filesystem once per invocation.
    """
    cwd_path = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    env_snapshot = tuple(
        sorted(
            (k, os.environ[k])
            for k in (*_BOOL_ENV_KEYS, *_STR_ENV_KEYS, "GODEL_HOME", "HOME")
            if k in os.environ
        )
    )
    cache_key = (str(cwd_path), env_snapshot)
    if use_cache and cache_key in _cache:
        return _cache[cache_key]

    sources: list[Path] = []
    merged: dict = {}

    user_settings = global_config_dir() / SETTINGS_FILENAME
    if user_settings.is_file():
        merged = _merge(merged, _read_json(user_settings))
        sources.append(user_settings)

    project_dir = find_project_config(cwd_path)
    project_root = project_dir.parent if project_dir else None
    if project_dir:
        project_settings = project_dir / SETTINGS_FILENAME
        if project_settings.is_file():
            merged = _merge(merged, _read_json(project_settings))
            sources.append(project_settings)

    merged = _merge(merged, _env_overrides())

    try:
        cfg = GodelConfig(**merged)
    except Exception as exc:
        raise ConfigError(f"invalid config: {exc}") from exc

    loaded = LoadedConfig(config=cfg, project_root=project_root, sources=sources)
    if use_cache:
        _cache[cache_key] = loaded
    return loaded


def clear_cache() -> None:
    """Clear the ``load_config`` memo table.  Primarily for tests."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Workflow resolution
# ---------------------------------------------------------------------------

def _workflow_search_dirs(loaded: LoadedConfig) -> list[Path]:
    dirs: list[Path] = []
    if loaded.project_root:
        p = Path(loaded.config.workflows_dir)
        if not p.is_absolute():
            p = loaded.project_root / p
        dirs.append(p)
    dirs.append(global_config_dir() / WORKFLOWS_SUBDIR)
    return dirs


def resolve_workflow(name_or_path: str, loaded: LoadedConfig) -> Path:
    """Resolve an arg to a workflow file path.

    1. Existing file path → returned as-is (today's behavior).
    2. Otherwise treat as a name; search project then user ``workflows/`` dir
       for ``<name>.py``.
    3. Raise ``ConfigError`` listing available names if not found.
    """
    p = Path(name_or_path)
    if p.is_file():
        return p.resolve()

    for base in _workflow_search_dirs(loaded):
        candidate = base / f"{name_or_path}.py"
        if candidate.is_file():
            return candidate.resolve()

    available = sorted(list_workflows(loaded).keys())
    hint = f"  available: {', '.join(available)}" if available else "  (no workflows registered)"
    raise ConfigError(
        f"workflow not found: {name_or_path!r}\n{hint}"
    )


def open_event_log(run_id: str, *, cwd: Path | str | None = None):
    """Open an existing ``EventLog`` for *run_id* using the configured runs dir.

    Convenience for programmatic callers so they don't hand-wire
    ``EventLog.load(run_id, runs_dir=str(load_config().runs_dir))``.
    """
    from godel._event_log import EventLog
    loaded = load_config(cwd)
    return EventLog.load(run_id, runs_dir=str(loaded.runs_dir))


def list_workflows(loaded: LoadedConfig) -> dict[str, Path]:
    """Return name → path for every ``.py`` file in the search dirs.

    Project workflows shadow user workflows on name collision.
    """
    found: dict[str, Path] = {}
    # Walk in reverse so earlier (project) entries overwrite later (user) ones.
    for base in reversed(_workflow_search_dirs(loaded)):
        if not base.is_dir():
            continue
        for f in base.glob("*.py"):
            if f.name.startswith("_"):
                continue
            found[f.stem] = f.resolve()
    return found

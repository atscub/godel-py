"""CLI entrypoint for godel."""
import asyncio
import importlib.util
import inspect
import os
import subprocess
import sys
import time
import traceback

import click

from godel._decorators import WorkflowFail
from godel._exceptions import PauseSignal


def _resolve_runs_dir(override: str | None = None) -> "Path":
    """Resolve the runs directory for CLI commands.

    Precedence: ``--runs-dir`` flag (override) > config-resolved path.
    """
    from pathlib import Path
    if override is not None:
        return Path(override)
    from godel._config import load_config
    return load_config().runs_dir


def parse_workflow_args(extra: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """Parse tokens after '--' into positional args and keyword args.

    Rules:
    - Token contains '=' AND the LHS is a valid Python identifier → kwarg
    - Otherwise → positional arg
    - Split on the FIRST '=' only, so ``q=a=b`` → key='q', value='a=b'
    - Key that is NOT a valid identifier (e.g. '1=foo') → treated as positional
    - Duplicate kwarg keys raise ValueError
    - All values are strings; workflow code is responsible for coercion

    Returns:
        (args, kwargs) where args is a list[str] and kwargs is a dict[str, str].
    """
    args: list[str] = []
    kwargs: dict[str, str] = {}
    seen_keys: set[str] = set()

    for token in extra:
        if "=" in token:
            lhs, _, rhs = token.partition("=")
            if lhs.isidentifier():
                if lhs in seen_keys:
                    raise ValueError(
                        f"Duplicate keyword argument '{lhs}' — "
                        f"each kwarg key must appear at most once."
                    )
                seen_keys.add(lhs)
                kwargs[lhs] = rhs
                continue
        # Not a kwarg — positional
        args.append(token)

    return args, kwargs


def _spawn_watch_subprocess(run_id: str, runs_dir: str, plain: bool = False) -> "subprocess.Popen":
    """Spawn ``python -m godel._watch <run_id>`` as an isolated subprocess.

    The subprocess is started in a **new process group** (``start_new_session``
    on POSIX) so that a renderer crash cannot propagate signals to the parent
    run process.  The caller owns the returned Popen handle and is responsible
    for joining or terminating it.

    Parameters
    ----------
    run_id:
        Workflow run identifier passed to the watcher.
    runs_dir:
        Path to the ``runs/`` directory (forwarded as ``--runs-dir``).
    plain:
        When ``True``, append ``--plain`` to the watcher command so the
        subprocess renders in plain line-log mode instead of the Rich TUI.

    Returns
    -------
    subprocess.Popen
    """
    cmd = [sys.executable, "-m", "godel._watch", run_id, "--runs-dir", runs_dir]
    if plain:
        cmd.append("--plain")
    # Isolate the watcher from the parent's console-control signals so a
    # terminal Ctrl+C (or a crashing renderer) cannot affect the underlying
    # run.  On POSIX ``start_new_session=True`` starts the child in a new
    # session/process group; on Windows the equivalent is the
    # CREATE_NEW_PROCESS_GROUP creation flag (Ctrl+C handling is inherited by
    # default on Windows, so the POSIX-only guard used previously was wrong).
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    # The watcher is a godel-internal subprocess, not a workflow operation, so
    # bypass the strict-mode audit hook that blocks subprocess.Popen.
    from godel._context import _privileged
    token = _privileged.set(True)
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        _privileged.reset(token)


def _run_workflow_with_sigint(fn, wf_args: list, wf_kwargs: dict):
    """Execute *fn* in a new event loop with proper SIGINT → cancellation handling.

    The first SIGINT cancels the running workflow task so that ``run()``
    primitives can clean up their process groups before exiting.  A second
    SIGINT arriving within one second of the first triggers an immediate
    ``os._exit(130)`` in case cleanup is hung.

    POSIX-only.  Windows is **out of scope** for this cleanup path: on Windows
    we skip installing the SIGINT handler, so a Ctrl+C there raises
    ``KeyboardInterrupt`` directly in the event loop thread and does NOT
    cancel the asyncio task — meaning ``run()``'s ``CancelledError`` handler
    does not fire and spawned subprocesses may be left alive.  Bridging
    Windows KeyboardInterrupt to ``Task.cancel()`` is tracked as a future
    enhancement; until then, Windows users should not rely on Ctrl+C for
    clean subprocess shutdown.
    """
    import signal as _signal

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    task: list = [None]  # mutable cell: [asyncio.Task | None]
    _sigint_count = [0]
    _first_sigint_time = [0.0]

    def _sigint_handler(signum, frame):
        import time as _time
        now = _time.monotonic()
        _sigint_count[0] += 1
        if _sigint_count[0] == 1:
            _first_sigint_time[0] = now
            # Cancel the workflow task — CancelledError will propagate into
            # run(), which will kill its process group before re-raising.
            t = task[0]
            if t is not None and not t.done():
                loop.call_soon_threadsafe(t.cancel)
        else:
            # Second SIGINT within 1 s of first → panic exit.
            if now - _first_sigint_time[0] < 1.0:
                os._exit(130)
            else:
                # > 1 s later — treat as a fresh first signal.
                _sigint_count[0] = 1
                _first_sigint_time[0] = now
                t = task[0]
                if t is not None and not t.done():
                    loop.call_soon_threadsafe(t.cancel)

    try:
        if sys.platform != "win32":
            old_handler = _signal.signal(_signal.SIGINT, _sigint_handler)
        else:
            old_handler = None

        async def _run():
            task[0] = asyncio.current_task()
            return await fn(*wf_args, **wf_kwargs)

        try:
            loop.run_until_complete(_run())
        except asyncio.CancelledError:
            # Surface as KeyboardInterrupt so the caller's except-branch fires.
            raise KeyboardInterrupt
        finally:
            if old_handler is not None:
                _signal.signal(_signal.SIGINT, old_handler)
    finally:
        try:
            # Cancel all remaining tasks before closing the loop.
            pending = asyncio.all_tasks(loop)
            if pending:
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)


@click.group()
def main():
    """Godel — deterministic orchestrator for AI agent workflows."""
    pass


@main.command("run", context_settings={"allow_extra_args": False, "ignore_unknown_options": True})
@click.argument("file")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@click.option("--no-strict", is_flag=True, help="Disable strict mode (allow non-deterministic ops)")
@click.option("--no-lint", is_flag=True, help="Skip lint pre-flight check")
@click.option("--watch", is_flag=True, help="Stream live output (requires godel[watch])")
@click.option(
    "--no-stream",
    is_flag=True,
    default=False,
    help="Disable agent-response streaming for this run (default: streaming enabled).",
)
@click.option(
    "--plain",
    "-p",
    is_flag=True,
    default=False,
    help="Force plain line-log output in the watcher subprocess (implies --watch; also: GODEL_WATCH_PLAIN=1).",
)
@click.option(
    "--auto-checkpoint",
    default=None,
    metavar="MODE",
    help=(
        "Declare that checkpoint answers arrive programmatically, not from a "
        "human terminal.  Sets GODEL_AUTO_CHECKPOINT=<MODE> so godel.input() "
        "tags events and suppresses the 'stdin is not a TTY' warning.  "
        "Use a descriptive value such as 'pipe', 'file', or 'fifo' (or '1' "
        "for generic scripting).  Passing an empty string (--auto-checkpoint=) "
        "explicitly clears any inherited GODEL_AUTO_CHECKPOINT env var and "
        "re-enables the warning.  Also: GODEL_AUTO_CHECKPOINT env var."
    ),
)
def run_cmd(file, extra, no_strict, no_lint, watch, no_stream, plain, auto_checkpoint):
    """Execute a @workflow-decorated function from FILE.

    Pass arguments to the workflow after a '--' separator:

    \b
        godel run FILE -- arg1 arg2 key=value

    Tokens containing '=' with a valid identifier LHS become keyword args;
    other tokens become positional args.  All values are passed as strings.

    FILE may be either a path to a .py file or the name of a workflow
    registered under ``<project>/.godel/workflows/`` or ``~/.godel/workflows/``.
    """
    # Resolve FILE to an actual path.  A real file path wins; otherwise treat
    # as a name and look it up in the configured workflows dirs.
    from pathlib import Path as _Path
    if not _Path(file).is_file():
        from godel._config import load_config, resolve_workflow
        from godel._exceptions import ConfigError
        try:
            resolved = resolve_workflow(file, load_config())
        except ConfigError as exc:
            click.echo(f"[godel] {exc}", err=True)
            sys.exit(2)
        click.echo(f"[godel] resolved workflow {file!r} -> {resolved}", err=True)
        file = str(resolved)

    if plain:
        watch = True
    if no_stream:
        os.environ["GODEL_STREAM_AGENTS"] = "0"
    if auto_checkpoint is not None:
        # Explicit --auto-checkpoint=<v> wins over any inherited env var.
        # An empty string clears the declaration (falls back to warning).
        os.environ["GODEL_AUTO_CHECKPOINT"] = auto_checkpoint
    if watch:
        try:
            from godel import _watch  # noqa: F401 — triggers import-time guard
        except Exception as exc:
            from godel._exceptions import GodelWatchNotInstalledError
            if isinstance(exc, GodelWatchNotInstalledError):
                click.echo(str(exc), err=True)
                sys.exit(1)
            raise

    if not no_strict:
        # Layer 1: AST pre-scan BEFORE loading the module
        from godel._strict_ast import scan_file
        from godel._exceptions import GodelStrictError

        violations = scan_file(file, raise_on_violation=False)
        if violations:
            err = GodelStrictError(violations)
            click.echo(str(err), err=True)
            sys.exit(1)

    # Pre-flight lint check: after strict AST scan, BEFORE import guard / audit hook
    # PL003 (non-determinism) is suppressed when --no-strict is set, because --no-strict
    # explicitly opts out of determinism enforcement — PL003 would otherwise make
    # --no-strict alone insufficient without --no-lint.
    if not no_lint:
        from godel._linter import lint_file
        skip_rules = {"PL003"} if no_strict else None
        diagnostics = lint_file(file, skip_rules=skip_rules)
        errors = [d for d in diagnostics if d.severity == "error"]
        if errors:
            click.echo("Lint errors found — refusing to run:", err=True)
            for d in errors:
                click.echo(click.style(f"  {d.format()}", fg="red"), err=True)
            click.echo("\nFix the errors or use --no-lint to skip.", err=True)
            sys.exit(1)
        # Print warnings but continue
        warnings = [d for d in diagnostics if d.severity == "warning"]
        for d in warnings:
            click.echo(click.style(f"  {d.format()}", fg="yellow"), err=True)

    if not no_strict:
        # Layer 2: Install import guard BEFORE exec_module
        from godel._strict_imports import install_import_guard
        install_import_guard()

        # Layer 3: Install audit hook BEFORE any runtime ops
        from godel._strict_audit import install_audit_hook
        install_audit_hook()

    # 1. Load the module
    spec = importlib.util.spec_from_file_location("_godel_workflow", file)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        if not no_strict:
            from godel._exceptions import GodelStrictError
            if isinstance(exc, GodelStrictError):
                click.echo(str(exc), err=True)
                sys.exit(1)
        click.echo(traceback.format_exc(), err=True)
        sys.exit(2)

    # 2. Discover @workflow function
    candidates = [
        v
        for v in vars(module).values()
        if callable(v) and getattr(v, "_is_workflow", False)
    ]
    if len(candidates) == 0:
        click.echo(f"No @workflow function found in {file}", err=True)
        sys.exit(2)
    if len(candidates) > 1:
        names = [f.__name__ for f in candidates]
        click.echo(
            f"Multiple @workflow functions found: {names} — not yet supported",
            err=True,
        )
        sys.exit(2)
    fn = candidates[0]
    fn._source_file = os.path.abspath(file)

    # 3. Parse workflow args from the extra tokens (after '--')
    try:
        wf_args, wf_kwargs = parse_workflow_args(extra)
    except ValueError as exc:
        click.echo(f"[godel] argument error: {exc}", err=True)
        sys.exit(2)

    # 4. Validate argument binding BEFORE emitting WORKFLOW_STARTED.
    #    The @workflow wrapper accepts *args/**kwargs unconditionally, so Python's
    #    argument binding never fails at the wrapper call — by the time a TypeError
    #    would propagate, the run has already started and a misleading resume hint
    #    would be printed.  Instead, we do a dry-run bind against the original
    #    (unwrapped) function here and emit a clean error with no resume hint.
    try:
        inspect.signature(fn.__wrapped__ if hasattr(fn, "__wrapped__") else fn).bind(
            *wf_args, **wf_kwargs
        )
    except TypeError as exc:
        click.echo(f"[godel] argument error: {exc}", err=True)
        sys.exit(2)

    # 5. Execute
    from godel._context import _on_run_start

    # Watcher subprocess handle (populated by _on_run_start when --watch is set).
    _watch_proc: list = [None]  # mutable cell: [subprocess.Popen | None]

    def _print_start(rid, log_path):
        click.echo(f"[godel] run {rid}", err=True)
        click.echo(f"[godel] audit log: {log_path}", err=True)
        if watch:
            # Derive the runs directory from the actual audit-log path so the
            # watcher subprocess looks in the correct transcript location even
            # when CWD != event-log root.
            from pathlib import Path as _Path
            _runs_dir = str(_Path(log_path).parent)
            _watch_proc[0] = _spawn_watch_subprocess(
                rid, runs_dir=_runs_dir, plain=plain,
            )

    def _drain_watcher() -> None:
        # Wait for the watcher subprocess to finish rendering before we print
        # our own status lines — otherwise the final agent events can appear
        # *after* "[godel] completed ...", since the watcher writes to stdout
        # while our status lines go to stderr with no ordering between them.
        proc = _watch_proc[0]
        if proc is None:
            return
        _watch_proc[0] = None
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    start_token = _on_run_start.set(_print_start)
    start = time.monotonic()
    try:
        _run_workflow_with_sigint(fn, wf_args, wf_kwargs)
        elapsed = time.monotonic() - start
        _drain_watcher()
        click.echo(f"[godel] completed in {elapsed:.1f}s", err=True)
        sys.exit(0)
    except PauseSignal:
        elapsed = time.monotonic() - start
        run_id = getattr(fn, "_last_run_id", None)
        _drain_watcher()
        click.echo(f"[godel] paused after {elapsed:.1f}s", err=True)
        if run_id:
            click.echo(f"[godel] resume with: godel resume {run_id}", err=True)
        sys.exit(0)
    except WorkflowFail as e:
        elapsed = time.monotonic() - start
        run_id = getattr(fn, "_last_run_id", None)
        _drain_watcher()
        click.echo(f"[godel] WorkflowFail after {elapsed:.1f}s: {e}", err=True)
        if run_id:
            click.echo(f"[godel] resume with: godel resume {run_id} {file}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        run_id = getattr(fn, "_last_run_id", None)
        _drain_watcher()
        click.echo("Interrupted", err=True)
        if run_id:
            click.echo(f"[godel] resume with: godel resume {run_id}", err=True)
        sys.exit(130)
    except Exception:
        elapsed = time.monotonic() - start
        run_id = getattr(fn, "_last_run_id", None)
        _drain_watcher()
        click.echo(f"[godel] unexpected error after {elapsed:.1f}s:", err=True)
        click.echo(traceback.format_exc(), err=True)
        if run_id:
            click.echo(f"[godel] resume with: godel resume {run_id}", err=True)
        sys.exit(2)
    finally:
        _on_run_start.reset(start_token)
        # Fallback: if an unexpected exit path skipped _drain_watcher (e.g.
        # SystemExit before the echoes), still reap the subprocess so it is
        # never stranded — renderer crashes must not leak processes.
        _drain_watcher()


@main.command("resume")
@click.argument("run_id")
@click.argument("file", type=click.Path(exists=True), required=False, default=None)
@click.option("--on-mismatch", type=click.Choice(["continue", "invalidate", "abort"]), default=None,
              help="Policy for request_hash mismatches")
@click.option("--on-source-edit", type=click.Choice(["warn", "abort", "ignore"]), default=None,
              help="Policy when a cached @step's source has been edited (default: warn)")
@click.option("--no-strict", is_flag=True, help="Disable strict mode (allow non-deterministic ops)")
@click.option("--no-lint", is_flag=True, help="Skip lint pre-flight check")
@click.option("--no-stream", is_flag=True, default=False,
              help="Disable agent-response streaming for this run (default: streaming enabled).")
@click.option(
    "--auto-checkpoint",
    default=None,
    metavar="MODE",
    help=(
        "Declare that checkpoint answers arrive programmatically.  "
        "Sets GODEL_AUTO_CHECKPOINT=<MODE>.  Use 'pipe', 'file', 'fifo', or "
        "'1' for generic scripting.  Passing an empty string "
        "(--auto-checkpoint=) clears any inherited env var.  "
        "Also: GODEL_AUTO_CHECKPOINT env var."
    ),
)
@click.option(
    "--assume-idempotent",
    is_flag=True,
    default=False,
    help=(
        "Treat ALL STARTED-only run()/agent() entries as safe to re-execute. "
        "Emits a WARNING for each promoted entry. "
        "Use when you are certain the interrupted operations had no irreversible side effects."
    ),
)
def resume_cmd(run_id, file, on_mismatch, on_source_edit, no_strict, no_lint, no_stream, auto_checkpoint, assume_idempotent):
    """Resume a workflow run from its audit log."""
    from pathlib import Path
    from godel._event_log import EventLog
    from godel._replay import (
        ReplayWalker, MismatchPolicy, set_mismatch_policy,
        SourceEditPolicy, set_source_edit_policy,
        set_assume_idempotent_all,
    )
    from godel._context import _pending_replay

    if no_stream:
        os.environ["GODEL_STREAM_AGENTS"] = "0"
    if auto_checkpoint is not None:
        # Explicit --auto-checkpoint=<v> wins over any inherited env var.
        os.environ["GODEL_AUTO_CHECKPOINT"] = auto_checkpoint

    # 1. Find JSONL by prefix
    runs_dir = _resolve_runs_dir()
    if not runs_dir.exists():
        click.echo("No runs/ directory found", err=True)
        sys.exit(1)

    matches = [f for f in runs_dir.glob("*.jsonl") if f.stem.startswith(run_id)]
    if len(matches) == 0:
        click.echo(f'No run matching "{run_id}"', err=True)
        sys.exit(1)
    if len(matches) > 1:
        stems = [f.stem for f in matches]
        click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
        sys.exit(1)

    full_run_id = matches[0].stem

    # 1b. Clear any pause sentinel so the first live @step after replay does
    # not immediately re-pause (idempotent — no-op if file is absent).
    from godel._pause import clear_pause_request
    clear_pause_request(full_run_id, runs_dir=str(runs_dir))

    # 2. Load EventLog and create ReplayWalker
    event_log = EventLog.load(full_run_id, runs_dir=str(runs_dir))
    walker = ReplayWalker(event_log)

    # 2b. Recover workflow args from WORKFLOW_STARTED event.
    #    get_workflow_args() raises ValueError when args_repr_only=True (non-serialisable
    #    args were used in the original run — programmatic resume only in that case).
    try:
        logged_wf_args = walker.get_workflow_args()
    except ValueError as exc:
        click.echo(f"[godel] resume error: {exc}", err=True)
        sys.exit(2)

    if file is None:
        file = logged_wf_args.get("source_file", "")
        if not file:
            click.echo("No file provided and no source_file in WORKFLOW_STARTED event. "
                        "Please provide the file argument.", err=True)
            sys.exit(2)
        if not os.path.exists(file):
            click.echo(f"source_file from audit log not found: {file}", err=True)
            sys.exit(2)

    # Recover original positional and keyword args logged at run time.
    # Structured args are stored as list/dict; repr-fallback raises above.
    wf_resume_args: list = logged_wf_args.get("args") or []
    wf_resume_kwargs: dict = logged_wf_args.get("kwargs") or {}
    if not isinstance(wf_resume_args, list):
        wf_resume_args = []
    if not isinstance(wf_resume_kwargs, dict):
        wf_resume_kwargs = {}

    # 3. Always reset mismatch policy to the module default (None = interactive)
    # before applying the CLI flag.  Without this reset, a previous CLI invocation
    # in the same Python process (e.g. a test suite or a REPL session) that called
    # set_mismatch_policy(ABORT) would silently bleed into this invocation.
    set_mismatch_policy(None)
    if on_mismatch:
        set_mismatch_policy(MismatchPolicy(on_mismatch))

    # 3c. Always reset source-edit policy to the module default (WARN) before
    # applying the CLI flag.  Without this reset, a previous CLI invocation in
    # the same Python process (e.g. a test suite or a REPL session) that called
    # set_source_edit_policy(ABORT) would silently bleed into this invocation.
    set_source_edit_policy(SourceEditPolicy.WARN)
    if on_source_edit:
        set_source_edit_policy(SourceEditPolicy(on_source_edit))

    # 3d. Reset assume-idempotent-all override (module-level global) and apply
    # the CLI flag.  Always reset first to prevent test-suite bleed.
    set_assume_idempotent_all(False)
    if assume_idempotent:
        # Emit a WARNING: the caller is opting into potentially unsafe re-execution.
        click.echo(
            click.style(
                "[godel] WARNING: --assume-idempotent is set. All STARTED-only "
                "run()/agent() entries will be re-executed without UnsafeResumeError. "
                "Only use this when you are certain these operations had no "
                "irreversible side effects.",
                fg="yellow",
            ),
            err=True,
        )
        set_assume_idempotent_all(True)

    # 3b. Strict mode: AST scan first (Layer 1)
    if not no_strict:
        from godel._strict_ast import scan_file
        from godel._exceptions import GodelStrictError

        violations = scan_file(file, raise_on_violation=False)
        if violations:
            err = GodelStrictError(violations)
            click.echo(str(err), err=True)
            sys.exit(1)

    # Pre-flight lint check: after strict AST scan, BEFORE import guard / audit hook
    # PL003 (non-determinism) is suppressed when --no-strict is set, because --no-strict
    # explicitly opts out of determinism enforcement — PL003 would otherwise make
    # --no-strict alone insufficient without --no-lint.
    if not no_lint:
        from godel._linter import lint_file
        skip_rules = {"PL003"} if no_strict else None
        diagnostics = lint_file(file, skip_rules=skip_rules)
        errors = [d for d in diagnostics if d.severity == "error"]
        if errors:
            click.echo("Lint errors found — refusing to run:", err=True)
            for d in errors:
                click.echo(click.style(f"  {d.format()}", fg="red"), err=True)
            click.echo("\nFix the errors or use --no-lint to skip.", err=True)
            sys.exit(1)
        # Print warnings but continue
        warnings = [d for d in diagnostics if d.severity == "warning"]
        for d in warnings:
            click.echo(click.style(f"  {d.format()}", fg="yellow"), err=True)

    if not no_strict:
        from godel._strict_imports import install_import_guard
        install_import_guard()

        from godel._strict_audit import install_audit_hook
        install_audit_hook()

    # 4. Set pending replay contextvar
    token = _pending_replay.set(walker)

    # 5. Load and discover workflow from file
    spec = importlib.util.spec_from_file_location("_godel_workflow", file)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        if not no_strict:
            from godel._exceptions import GodelStrictError
            if isinstance(exc, GodelStrictError):
                click.echo(str(exc), err=True)
                _pending_replay.reset(token)
                sys.exit(1)
        click.echo(traceback.format_exc(), err=True)
        _pending_replay.reset(token)
        sys.exit(2)

    candidates = [
        v
        for v in vars(module).values()
        if callable(v) and getattr(v, "_is_workflow", False)
    ]
    if len(candidates) == 0:
        click.echo(f"No @workflow function found in {file}", err=True)
        _pending_replay.reset(token)
        sys.exit(2)
    if len(candidates) > 1:
        names = [f.__name__ for f in candidates]
        click.echo(
            f"Multiple @workflow functions found: {names} — not yet supported",
            err=True,
        )
        _pending_replay.reset(token)
        sys.exit(2)
    fn = candidates[0]

    # 6. Execute with replay (using original args recovered from WORKFLOW_STARTED)
    from godel._context import _on_run_start

    def _print_start(rid, log_path):
        click.echo(f"[godel] run {rid}", err=True)
        click.echo(f"[godel] audit log: {log_path}", err=True)

    start_token = _on_run_start.set(_print_start)
    start = time.monotonic()
    try:
        asyncio.run(fn(*wf_resume_args, **wf_resume_kwargs))
        elapsed = time.monotonic() - start
        click.echo(f"[godel] resumed run completed in {elapsed:.1f}s", err=True)
        sys.exit(0)
    except PauseSignal:
        elapsed = time.monotonic() - start
        click.echo(f"[godel] paused after {elapsed:.1f}s", err=True)
        click.echo(f"[godel] resume with: godel resume {full_run_id}", err=True)
        sys.exit(0)
    except WorkflowFail as e:
        elapsed = time.monotonic() - start
        click.echo(f"[godel] WorkflowFail after {elapsed:.1f}s: {e}", err=True)
        click.echo(f"[godel] resume with: godel resume {full_run_id}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("Interrupted", err=True)
        click.echo(f"[godel] resume with: godel resume {full_run_id}", err=True)
        sys.exit(130)
    except Exception:
        elapsed = time.monotonic() - start
        click.echo(f"[godel] unexpected error after {elapsed:.1f}s:", err=True)
        click.echo(traceback.format_exc(), err=True)
        click.echo(f"[godel] resume with: godel resume {full_run_id}", err=True)
        sys.exit(2)
    finally:
        _on_run_start.reset(start_token)
        _pending_replay.reset(token)
        # Always reset global assume-idempotent-all to prevent bleed into
        # subsequent calls in the same process (e.g. test suites).
        set_assume_idempotent_all(False)


@main.command("show")
@click.argument("run_id")
@click.option("--graph", is_flag=True, help="Render DAG as ASCII tree")
@click.option("--all", "show_all", is_flag=True, help="Show failed retries and invalidated events")
def show_cmd(run_id, graph, show_all):
    """Display the audit log for a workflow run."""
    from pathlib import Path
    from godel._event_log import EventLog

    runs_dir = _resolve_runs_dir()
    if not runs_dir.exists():
        click.echo("No runs/ directory found", err=True)
        sys.exit(1)

    matches = [f for f in runs_dir.glob("*.jsonl") if f.stem.startswith(run_id)]
    if len(matches) == 0:
        click.echo(f'No run matching "{run_id}"', err=True)
        sys.exit(1)
    if len(matches) > 1:
        stems = [f.stem for f in matches]
        click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
        sys.exit(1)

    log = EventLog.load(matches[0].stem, runs_dir=str(runs_dir))

    if graph:
        from godel._dag_render import render_dag
        for text, color, dim in render_dag(log.all_events(), show_all=show_all):
            if color:
                click.echo(click.style(text, fg=color, dim=dim))
            else:
                click.echo(text)
    else:
        _show_list(log.all_events(), show_all)

    log.close()


_STATUS_COLOR = {
    "FINISHED": "green",
    "FAILED": "red",
    "STARTED": "yellow",
    "INVALIDATED": "magenta",
    "SUSPENDED": "cyan",
    "PAUSED": "cyan",
}


def _fmt_event(event) -> str:
    """Format a single Event as a one-line human-readable string."""
    from godel._formatters import FORMATTERS, _default_formatter
    return FORMATTERS.get(event.op, _default_formatter)(event)


def _show_list(events, show_all):
    """Render events as a colored list, with optional retry/invalidated grouping."""
    from godel._dag_render import _partition_events, _step_key

    effective, retries, invalidated = _partition_events(events)

    for event in effective:
        key = _step_key(event)
        # Show prior failures grouped before the successful event
        if show_all and key in retries:
            n = len(retries[key])
            click.echo(click.style(f"  \u250c\u2500 \u2717 {n} prior attempt(s):", fg="red", dim=True))
            for fe in retries[key]:
                line = _fmt_event(fe)
                click.echo(click.style(f"  \u2502  {line}", fg="red", dim=True))
            click.echo(click.style(f"  \u2514\u2500 succeeded:", fg="red", dim=True))

        color = _STATUS_COLOR.get(event.status.value, "white")
        click.echo(click.style(_fmt_event(event), fg=color))

    # Invalidated subgraph
    if show_all and invalidated:
        click.echo()
        click.echo(click.style("\u2504\u2504\u2504 invalidated (rewind) \u2504\u2504\u2504", fg="magenta", dim=True))
        for event in invalidated:
            line = _fmt_event(event)
            click.echo(click.style(f"  \u2298 {line}", fg="magenta", dim=True))


@main.command("lint")
@click.argument("file", type=click.Path())
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text",
              help="Output format")
@click.option("--skip", default="", help="Comma-separated rule IDs to skip (e.g. PL003,PL007)")
def lint_cmd(file, output_format, skip):
    """Lint a workflow file for common mistakes."""
    import json as json_mod
    from godel._linter import lint_file, get_rules

    skip_rules = {s.strip() for s in skip.split(",") if s.strip()} if skip else None

    # Warn about unknown rule IDs in --skip before running the linter
    if skip_rules:
        known_ids = {rule.rule_id for rule in get_rules()}
        unknown = skip_rules - known_ids
        for uid in sorted(unknown):
            click.echo(
                click.style(f"Warning: unknown rule ID '{uid}' in --skip (ignored)", fg="yellow"),
                err=True,
            )

    diagnostics = lint_file(file, skip_rules=skip_rules)

    if output_format == "json":
        click.echo(json_mod.dumps([d.to_dict() for d in diagnostics], indent=2))
    else:
        for d in diagnostics:
            color = "red" if d.severity == "error" else "yellow"
            click.echo(click.style(d.format(), fg=color))

    # Exit code: 1 if any errors, 0 if warnings only or clean
    has_errors = any(d.severity == "error" for d in diagnostics)
    if has_errors:
        error_count = sum(1 for d in diagnostics if d.severity == "error")
        warning_count = sum(1 for d in diagnostics if d.severity == "warning")
        click.echo(f"\n{error_count} error(s), {warning_count} warning(s)", err=True)
        sys.exit(1)
    elif diagnostics:
        click.echo(f"\n{len(diagnostics)} warning(s)", err=True)


@main.command("pause")
@click.argument("run_id")
@click.option("--reason", default="CLI pause", help="Reason for the pause")
def pause_cmd(run_id, reason):
    """Request a live workflow run to pause at its next @step boundary."""
    from godel._pause import pause as pause_api
    try:
        full = pause_api(run_id, reason=reason)
    except FileNotFoundError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(3)
    click.echo(f"[godel] pause requested for run {full}", err=True)
    click.echo(f"[godel] sentinel: runs/{full}.pause", err=True)
    click.echo(f"[godel] resume with: godel resume {full}", err=True)


@main.command("rewind")
@click.argument("run_id")
@click.option("--to", "target_ids", required=True,
              help="Comma-separated event ID(s) to rewind to")
@click.option("--reason", default="CLI rewind", help="Reason for the rewind")
def rewind_cmd(run_id, target_ids, reason):
    """Rewind a workflow run to a previous checkpoint."""
    from pathlib import Path
    from godel._event_log import EventLog
    from godel._exceptions import RewindUnsafe
    from godel._rewind import apply_rewind

    # 1. Find JSONL by prefix (same pattern as resume_cmd)
    runs_dir = _resolve_runs_dir()
    if not runs_dir.exists():
        click.echo("No runs/ directory found", err=True)
        sys.exit(1)

    matches = [f for f in runs_dir.glob("*.jsonl") if f.stem.startswith(run_id)]
    if len(matches) == 0:
        click.echo(f'No run matching "{run_id}"', err=True)
        sys.exit(1)
    if len(matches) > 1:
        stems = [f.stem for f in matches]
        click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
        sys.exit(3)

    full_run_id = matches[0].stem

    # 2. Load EventLog
    event_log = EventLog.load(full_run_id, runs_dir=str(runs_dir))

    try:
        # 3. Parse comma-separated event IDs
        ids = [t.strip() for t in target_ids.split(",") if t.strip()]
        if not ids:
            click.echo("No event IDs provided", err=True)
            sys.exit(1)

        # 4. Validate event IDs exist
        for eid in ids:
            if event_log.get_event(eid) is None:
                click.echo(f"Event ID not found: {eid}", err=True)
                sys.exit(1)

        # 5. Capture op names BEFORE apply_rewind() mutates the graph
        op_map: dict[str, str] = {}
        for ev in event_log.all_events():
            op_map[ev.event_id] = ev.op

        # 6. Apply rewind
        try:
            result = apply_rewind(event_log, ids, reason)
        except RewindUnsafe as exc:
            detail_parts = [f"Rewind failed: {exc}"]
            if exc.event_id:
                detail_parts.append(f"  blocking event: {exc.event_id}")
            if exc.cmd:
                detail_parts.append(f"  command: {exc.cmd}")
            click.echo("\n".join(detail_parts), err=True)
            sys.exit(2)
        except Exception as exc:
            click.echo(f"Rewind failed: {exc}", err=True)
            sys.exit(1)

        # 7. Print summary — all primary output goes to stderr (matches run_cmd convention)
        click.echo(f"[godel] rewound run {full_run_id}", err=True)
        click.echo(f"[godel] invalidated {result['invalidated_count']} event(s)", err=True)
        if result.get("invalidated_ids"):
            for eid in result["invalidated_ids"][:10]:  # Show first 10
                op = op_map.get(eid, "?")
                short_id = f"{eid[:8]}..." if len(eid) > 8 else eid
                click.echo(f"  - {short_id} ({op})", err=True)
            if len(result["invalidated_ids"]) > 10:
                click.echo(f"  ... and {len(result['invalidated_ids']) - 10} more", err=True)
        click.echo(f"[godel] resume with: godel resume {full_run_id}", err=True)
    finally:
        event_log.close()


@main.command("repair")
@click.argument("run_id")
@click.option("--agent", "agent_spec", default=None,
              help='Python path to a custom intervention @workflow, e.g. "mypkg.mod:my_agent"')
@click.option("--model", default="opus")
@click.option("--max-iterations", type=int, default=8)
@click.option("--dry-run", is_flag=True,
              help="Build context and print, but do not invoke the agent")
def repair_cmd(run_id, agent_spec, model, max_iterations, dry_run):
    """Drop an intervention agent into a paused or crashed run."""
    import importlib
    from pathlib import Path
    from godel.intervention import build_intervention_context, InterventionToolset
    from godel.intervention._tools import ResumeRequested, GaveUp

    # 1. Prefix resolution
    runs_dir = _resolve_runs_dir()
    if not runs_dir.exists():
        click.echo("No runs/ directory found", err=True)
        sys.exit(1)

    matches = [f for f in runs_dir.glob("*.jsonl") if f.stem.startswith(run_id)]
    if len(matches) == 0:
        click.echo(f'No run matching "{run_id}"', err=True)
        sys.exit(1)
    if len(matches) > 1:
        stems = [f.stem for f in matches]
        click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
        sys.exit(1)

    full_run_id = matches[0].stem

    # 2. Build intervention context
    ctx = build_intervention_context(full_run_id, runs_dir=str(runs_dir))

    # 3. State guard
    if ctx.run_state not in ("PAUSED", "FAILED"):
        click.echo(
            f"[godel] refusing to repair run in state {ctx.run_state!r} "
            f"(expected PAUSED or FAILED)",
            err=True,
        )
        sys.exit(2)

    click.echo(f"[godel] repairing run {full_run_id} (state: {ctx.run_state})", err=True)
    if ctx.failure:
        click.echo(
            f"[godel] failure: {ctx.failure.error_type}: {ctx.failure.error}",
            err=True,
        )

    # 4. Dry-run path
    if dry_run:
        click.echo(ctx.to_json())
        sys.exit(0)

    # 5. Resolve intervention agent
    tools = InterventionToolset(ctx, runs_dir=str(runs_dir))

    if agent_spec:
        if ":" not in agent_spec:
            click.echo("--agent must be MODULE:FUNCTION", err=True)
            sys.exit(2)
        mod_path, _, attr = agent_spec.rpartition(":")
        try:
            module = importlib.import_module(mod_path)
        except ImportError as exc:
            click.echo(f"Failed to import {mod_path!r}: {exc}", err=True)
            sys.exit(2)
        fn = getattr(module, attr, None)
        if fn is None:
            click.echo(f"{mod_path} has no attribute {attr!r}", err=True)
            sys.exit(2)
        if not getattr(fn, "_is_workflow", False):
            click.echo(f"{agent_spec} is not a @workflow-decorated function", err=True)
            sys.exit(2)
    else:
        from godel.intervention.default_agent import default_intervention_agent as fn

    # 6. Invoke agent
    start = time.monotonic()
    try:
        outcome = asyncio.run(fn(ctx, tools, model=model, max_iterations=max_iterations))
    except ResumeRequested as r:
        outcome = {"outcome": "resume", "reason": r.reason}
    except GaveUp as g:
        outcome = {"outcome": "give_up", "reason": g.reason}
    except Exception:
        click.echo(
            f"[godel] intervention agent crashed after {time.monotonic() - start:.1f}s:",
            err=True,
        )
        click.echo(traceback.format_exc(), err=True)
        sys.exit(3)

    elapsed = time.monotonic() - start

    # 7. Outcome dispatch
    result = outcome.get("outcome") if isinstance(outcome, dict) else None
    if result == "resume":
        click.echo(
            f"[godel] intervention complete in {elapsed:.1f}s: resume ({outcome.get('reason', '')})",
            err=True,
        )
        click.echo(f"[godel] run `godel resume {full_run_id}` to continue", err=True)
        sys.exit(0)
    if result == "give_up":
        click.echo(
            f"[godel] intervention gave up after {elapsed:.1f}s: {outcome.get('reason', '')}",
            err=True,
        )
        sys.exit(1)
    click.echo(
        f"[godel] intervention returned unexpected outcome: {outcome!r}",
        err=True,
    )
    sys.exit(3)


@main.command("tail")
@click.argument("run_id")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["pretty", "json"]),
    default="pretty",
    help="Output format: pretty (default) or raw JSON",
)
@click.option(
    "--no-follow",
    is_flag=True,
    help="Exit at EOF instead of waiting for new events",
)
@click.option(
    "--no-wait",
    is_flag=True,
    help="Fail immediately if the log file does not exist yet",
)
def tail_cmd(run_id, output_format, no_follow, no_wait):
    """Follow a workflow's audit log in real time."""
    import json as json_mod
    from pathlib import Path
    from godel._tail import tail as _tail

    runs_dir = _resolve_runs_dir()

    if no_wait:
        # Resolve prefix and fail fast if the file isn't there yet
        matches = list(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
        matches = [f for f in matches if f.stem.startswith(run_id)]
        if not matches:
            click.echo(f'No run matching "{run_id}"', err=True)
            sys.exit(1)
        if len(matches) > 1:
            stems = [f.stem for f in matches]
            click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
            sys.exit(1)

    async def _go():
        try:
            async for event in _tail(
                run_id,
                runs_dir=runs_dir,
                follow=not no_follow,
            ):
                if output_format == "json":
                    click.echo(json_mod.dumps(event.to_dict(), separators=(",", ":")))
                else:
                    color = _STATUS_COLOR.get(event.status.value, "white")
                    click.echo(click.style(_fmt_event(event), fg=color))
        except ValueError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        sys.exit(130)


# ---------------------------------------------------------------------------
# Discoverability hint helper
# ---------------------------------------------------------------------------

_STREAM_AGENTS_HINT = (
    "agent streaming was disabled for this run (--no-stream); "
    "re-run without --no-stream to enable live streaming. "
    "See docs/transcript-format.md"
)

_HINT_PROBE_TIMEOUT = 5.0  # seconds to wait before showing the hint


def _check_stream_agents_disabled(run_id: str, runs_dir: str) -> bool:
    """Return True if streaming is disabled for *run_id*.

    Streaming is enabled by default; a transcript directory exists under
    ``<runs_dir>/<run_id>/`` when streaming is on.  When that directory is
    absent, the run was executed with ``--no-stream`` and the discoverability
    hint should be shown.

    Returns True (hint should show) when no transcript directory is found.
    Returns False (hint should NOT show) when the directory exists — safe default.
    """
    from pathlib import Path
    run_dir = Path(runs_dir) / run_id
    # If the transcript directory exists, streaming was enabled.
    return not run_dir.exists()


# ---------------------------------------------------------------------------
# `godel watch` subcommand
# ---------------------------------------------------------------------------


@main.command("watch")
@click.argument("run_id")
@click.option(
    "--runs-dir",
    default=None,
    help="Directory containing per-run transcript directories (default: resolved from config)",
)
@click.option(
    "--plain",
    "-p",
    is_flag=True,
    default=False,
    help="Force plain line-log output instead of the Rich TUI (also: GODEL_WATCH_PLAIN=1).",
)
def watch_cmd(run_id, runs_dir, plain):
    """Attach a live TUI renderer to a running or completed workflow.

    Replays history from archived transcript files then follows the live
    transcript until the run finishes or Ctrl+C is pressed.

    Can be used while a run is in progress (late-attach) or after it has
    completed (replay viewer — exits automatically after the final event).

    Requires godel[watch] (pip install 'godel[watch]').
    """
    from pathlib import Path

    # Guard: require rich
    try:
        from godel._watch import run_watch  # noqa: F401
    except Exception as exc:
        from godel._exceptions import GodelWatchNotInstalledError
        if isinstance(exc, GodelWatchNotInstalledError):
            click.echo(str(exc), err=True)
            sys.exit(1)
        raise

    runs_path = _resolve_runs_dir(runs_dir)
    runs_dir = str(runs_path)
    if not runs_path.exists():
        click.echo(f'No runs directory found at "{runs_dir}"', err=True)
        sys.exit(1)

    candidates: set[str] = set()
    for f in runs_path.glob("*.jsonl"):
        if f.stem.startswith(run_id):
            candidates.add(f.stem)
    for d in runs_path.iterdir():
        if d.is_dir() and d.name.startswith(run_id):
            candidates.add(d.name)

    if not candidates:
        click.echo(f'No run matching "{run_id}"', err=True)
        sys.exit(1)
    if len(candidates) > 1:
        stems = sorted(candidates)
        click.echo(f'Ambiguous prefix "{run_id}" — matches: {stems}', err=True)
        sys.exit(1)
    run_id = next(iter(candidates))

    # Discoverability hint: show banner if the run was executed with
    # --no-stream (no transcript dir).  Emitted on stderr within 6 s per AC.
    streaming_disabled = _check_stream_agents_disabled(run_id, runs_dir)
    if streaming_disabled:
        click.echo(
            click.style(f"[godel-watch] hint: {_STREAM_AGENTS_HINT}", fg="yellow"),
            err=True,
        )
        # No transcript to follow — exit immediately rather than hanging.
        sys.exit(0)

    try:
        run_watch(run_id, runs_dir=runs_dir, plain=plain)
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# `godel init` — scaffold .godel/ in the current project
# ---------------------------------------------------------------------------

_INIT_SETTINGS_STUB = """{
  "runs_dir": null,
  "workflows_dir": ".godel/workflows",
  "strict": true,
  "lint": true,
  "stream_agents": true
}
"""


@main.command("init")
def init_cmd():
    """Scaffold a ``.godel/`` directory in the current project.

    Idempotent — existing files are never overwritten; each file prints
    ``created`` or ``exists, skipped``.
    """
    from pathlib import Path
    from godel._config import CONFIG_DIR_NAME, SETTINGS_FILENAME, WORKFLOWS_SUBDIR

    root = Path.cwd() / CONFIG_DIR_NAME
    workflows = root / WORKFLOWS_SUBDIR
    settings = root / SETTINGS_FILENAME

    if not root.exists():
        root.mkdir()
        click.echo(f"created {root}")
    else:
        click.echo(f"exists, skipped {root}")
    if not workflows.exists():
        workflows.mkdir(parents=True)
        click.echo(f"created {workflows}")
    else:
        click.echo(f"exists, skipped {workflows}")

    if not settings.exists():
        settings.write_text(_INIT_SETTINGS_STUB)
        click.echo(f"created {settings}")
    else:
        click.echo(f"exists, skipped {settings}")


# ---------------------------------------------------------------------------
# `godel config` — inspect merged configuration
# ---------------------------------------------------------------------------


@main.group("config")
def config_group():
    """Inspect godel configuration."""


@config_group.command("path")
def config_path_cmd():
    """Print config sources in precedence order with the effective merged view."""
    import json as _json
    from godel._config import load_config, global_config_dir, find_project_config

    loaded = load_config()
    global_settings = global_config_dir() / "settings.json"
    project_dir = find_project_config()
    project_settings = (project_dir / "settings.json") if project_dir else None

    click.echo("sources (low -> high precedence):")
    mark = lambda p: "✓" if (p and p.is_file()) else "✗"
    click.echo(f"  {mark(global_settings)} {global_settings}")
    if project_settings:
        click.echo(f"  {mark(project_settings)} {project_settings}")
    else:
        click.echo("  (no project .godel/ found)")
    click.echo(f"\nproject_root: {loaded.project_root}")
    click.echo(f"runs_dir:     {loaded.runs_dir}")
    click.echo("\neffective config:")
    click.echo(_json.dumps(loaded.config.model_dump(), indent=2))


# ---------------------------------------------------------------------------
# `godel workflows` — list and resolve named workflows
# ---------------------------------------------------------------------------


@main.group("workflows")
def workflows_group():
    """List and resolve named workflows."""


@workflows_group.command("list")
def workflows_list_cmd():
    """List every named workflow discoverable from the current cwd."""
    from godel._config import load_config, list_workflows

    loaded = load_config()
    found = list_workflows(loaded)
    if not found:
        click.echo("(no workflows found)")
        return
    for name in sorted(found):
        click.echo(f"  {name}  -> {found[name]}")


@workflows_group.command("which")
@click.argument("name")
def workflows_which_cmd(name):
    """Print the resolved path for NAME without running it."""
    from godel._config import load_config, resolve_workflow
    from godel._exceptions import ConfigError

    try:
        path = resolve_workflow(name, load_config())
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(str(path))


# ---------------------------------------------------------------------------
# `godel guide` — bundled on-demand docs for agents
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `godel runs` — enumerate past runs
# ---------------------------------------------------------------------------


@main.group("runs", invoke_without_command=True)
@click.pass_context
def runs_group(ctx):
    """Manage and inspect past workflow runs."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _format_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


@runs_group.command("list")
@click.option("--status", default=None, help="Filter by status (running/finished/failed/paused)")
@click.option("--limit", default=None, type=int, help="Max number of rows to show")
@click.option("--runs-dir", "runs_dir_override", default=None, help="Override runs directory")
def runs_list_cmd(status, limit, runs_dir_override):
    """List past workflow runs with status and duration."""
    from godel._run_summary import summarize_run

    runs_dir = _resolve_runs_dir(runs_dir_override)
    if not runs_dir.exists():
        click.echo(f"Runs directory not found: {runs_dir}", err=True)
        sys.exit(1)

    jsonl_files = sorted(runs_dir.glob("*.jsonl"))
    summaries = []
    for f in jsonl_files:
        summaries.append(summarize_run(f))

    # Sort by ts_start descending (most recent first); empty ts_start goes last
    summaries.sort(key=lambda s: s.ts_start or "", reverse=True)

    if status is not None:
        status_upper = status.upper()
        summaries = [s for s in summaries if s.status.upper() == status_upper]

    if limit is not None:
        summaries = summaries[:limit]

    # Print table
    col_id = 28
    col_wf = 20
    col_st = 10
    col_ts = 20
    col_du = 10
    header = (
        f"{'RUN ID':<{col_id}}  "
        f"{'WORKFLOW':<{col_wf}}  "
        f"{'STATUS':<{col_st}}  "
        f"{'STARTED':<{col_ts}}  "
        f"DURATION"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for s in summaries:
        rid = s.run_id[:col_id]
        wf = s.workflow_name[:col_wf]
        st = s.status[:col_st]
        ts = _format_ts(s.ts_start)[:col_ts]
        dur = _format_duration(s.duration_s)
        click.echo(
            f"{rid:<{col_id}}  {wf:<{col_wf}}  {st:<{col_st}}  {ts:<{col_ts}}  {dur}"
        )


@main.command("guide")
@click.argument("name", required=False)
def guide_cmd(name):
    """Print a bundled guide, or list available guides with no argument.

    Intended for agents to pull just-in-time onboarding content without the
    godel repo being available locally.
    """
    from importlib import resources
    from godel._guides import GUIDES, GODEL_BLURB

    if name is None:
        click.echo(GODEL_BLURB)
        click.echo("Available guides (use `godel guide <name>` to read one):\n")
        width = max(len(slug) for slug, _ in GUIDES)
        for slug, hook in GUIDES:
            click.echo(f"  {slug:<{width}}  {hook}")
        return

    slugs = {slug for slug, _ in GUIDES}
    if name not in slugs:
        click.echo(f"unknown guide: {name!r}", err=True)
        click.echo(f"available: {', '.join(sorted(slugs))}", err=True)
        sys.exit(1)

    try:
        text = resources.files("godel._guides").joinpath(f"{name}.md").read_text()
    except FileNotFoundError as exc:
        click.echo(f"guide {name!r} is registered but not bundled: {exc}", err=True)
        sys.exit(1)
    click.echo(text)

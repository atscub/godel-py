"""Live-watch rendering for workflow runs.

This module requires the ``watch`` optional dependency (``rich``).  Import is
guarded so that ``godel`` core remains importable without the extra installed.

Install with::

    pip install 'godel[watch]'

Public entry point
------------------
- :func:`run_watch` — start the live TUI (or plain line-log on dumb terminals).
- :class:`WatchApp` — renderable shell; useful for snapshot tests via
  ``WatchApp._render(model, console)``.

Fallback behaviour
------------------
The Rich live display is disabled and a plain prefixed line-log is used instead
when any of the following conditions are true:

* ``sys.stdout.isatty()`` is ``False``
* The ``TERM`` environment variable equals ``"dumb"``
* The locale encoding is not UTF-8 (checked via ``locale.getpreferredencoding``)

Terminal hazards
----------------
``SIGTSTP`` (Ctrl+Z) and ``SIGHUP`` (terminal drop) are intercepted.  Before
delegating to the default disposition the signal handler calls
``Live.stop()`` so the terminal is restored (cursor visible, colours reset).
Rich handles ``SIGWINCH`` automatically.
"""
from __future__ import annotations

import locale
import os
import queue
import signal
import sys
import threading
import time
from typing import IO

from godel._exceptions import GodelWatchNotInstalledError

try:
    import rich  # noqa: F401
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.tree import Tree
except ImportError as _e:
    raise GodelWatchNotInstalledError(
        "godel --watch requires 'rich'. Install with: pip install 'godel[watch]'"
    ) from _e

from godel._watch_model import WatchModel, StreamPanel, StepNode, reduce, reduce_header


# ---------------------------------------------------------------------------
# Coalescing constants
# ---------------------------------------------------------------------------

_BURST_THRESHOLD = 25   # trigger render if >= this many events queued
_TIMER_INTERVAL = 0.1   # seconds between timer-triggered renders


# ---------------------------------------------------------------------------
# Fallback detection
# ---------------------------------------------------------------------------

def _use_plain_fallback(stdout: IO | None = None) -> bool:
    """Return True if we should use the plain line-log instead of Rich TUI."""
    fh = stdout if stdout is not None else sys.stdout

    # Non-TTY
    if not getattr(fh, "isatty", lambda: False)():
        return True

    # TERM=dumb
    if os.environ.get("TERM", "").lower() == "dumb":
        return True

    # Non-UTF-8 locale
    try:
        enc = locale.getpreferredencoding(False)
    except Exception:
        enc = ""
    if enc and "utf" not in enc.lower():
        return True

    return False


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    "running": "bold yellow",
    "done": "bold green",
    "failed": "bold red",
    "pending": "dim",
}

_STATUS_ICON = {
    "running": "⏳",
    "done": "✓",
    "failed": "✗",
    "pending": "○",
}


def _step_label(node: StepNode) -> Text:
    """Build a rich Text label for a StepNode."""
    status = node.status
    icon = _STATUS_ICON.get(status, "?")
    style = _STATUS_STYLE.get(status, "")
    name = node.path[-1] if node.path else "(root)"
    label = Text()
    label.append(f"{icon} {name}", style=style)
    if node.started_at and status == "running":
        label.append(f"  [{node.started_at[:19]}]", style="dim")
    elif node.finished_at:
        label.append(f"  [{node.finished_at[:19]}]", style="dim")
    return label


def _build_tree(model: WatchModel) -> Tree:
    """Build a rich Tree from WatchModel.steps."""
    run_id = model.run_meta.get("run_id", "run")
    root = Tree(Text(str(run_id), style="bold cyan"))

    # Collect top-level paths and build hierarchically by path length.
    # We group by parent path — simple approach for 1-2 levels deep.
    top_nodes: list[StepNode] = sorted(
        [n for n in model.steps.values() if len(n.path) == 1],
        key=lambda n: n.started_at or "",
    )

    def _add_children(tree_node: Tree, parent_path: tuple) -> None:
        children = sorted(
            [n for n in model.steps.values() if n.path[:-1] == parent_path],
            key=lambda n: n.started_at or "",
        )
        for child in children:
            branch = tree_node.add(_step_label(child))
            _add_children(branch, child.path)

    for step in top_nodes:
        branch = root.add(_step_label(step))
        _add_children(branch, step.path)

    return root


def _panel_title(sp: StreamPanel) -> str:
    return "/".join(sp.stream_path) if sp.stream_path else "stream"


def _build_panels_renderable(model: WatchModel, *, max_inline: int = 3):
    """Build a renderable for the panels pane.

    Up to *max_inline* active panels are stacked.  If there are more, the
    overflow panels are shown in a tabbed summary (panel titles only, content
    truncated to one line).
    """
    from rich.columns import Columns
    from rich.table import Table

    active = sorted(
        model.panels.values(),
        key=lambda p: p.last_event_ts or "",
        reverse=True,
    )

    if not active:
        return Panel(Text("(no output yet)", style="dim"), title="streams")

    inline = active[:max_inline]
    overflow = active[max_inline:]

    renderables = []
    for panel in inline:
        lines = "\n".join(panel.ring[-20:]) if panel.ring else "(empty)"
        renderables.append(Panel(Text(lines), title=_panel_title(panel), expand=True))

    if overflow:
        tab_lines = []
        for p in overflow:
            last = p.ring[-1] if p.ring else "(empty)"
            tab_lines.append(f"[dim]{_panel_title(p)}[/dim]: {last[:60]}")
        tab_text = "\n".join(tab_lines)
        renderables.append(Panel(Text.from_markup(tab_text), title=f"+{len(overflow)} more", expand=True))

    if len(renderables) == 1:
        return renderables[0]

    # Stack vertically via a simple Table column
    tbl = Table.grid(expand=True)
    tbl.add_column()
    for r in renderables:
        tbl.add_row(r)
    return tbl


# ---------------------------------------------------------------------------
# WatchApp — renderable model presenter
# ---------------------------------------------------------------------------

class WatchApp:
    """Drives a ``rich.live.Live`` display, observing :class:`WatchModel`.

    Parameters
    ----------
    run_id:
        The workflow run identifier (used as the display title).
    console:
        Optional Rich ``Console`` to use.  If ``None`` one is created.

    Notes
    -----
    ``_render(model, console)`` is a **static** helper so tests can call it
    directly without constructing a full ``WatchApp``.
    """

    def __init__(self, run_id: str, *, console: Console | None = None) -> None:
        self.run_id = run_id
        self.console = console or Console()
        self._live: Live | None = None
        self._model = WatchModel.empty()

    # ------------------------------------------------------------------
    # Public static helper — primary target for snapshot tests
    # ------------------------------------------------------------------

    @staticmethod
    def _render(model: WatchModel, console: Console) -> None:
        """Render *model* once to *console* (no live display).

        Useful for snapshot tests::

            console = Console(record=True)
            WatchApp._render(model, console)
            output = console.export_text()
        """
        layout = WatchApp._build_layout(model)
        console.print(layout)

    @staticmethod
    def _build_layout(model: WatchModel) -> Layout:
        """Build a Layout from *model* (pure, no console side-effects)."""
        layout = Layout(name="root")
        layout.split_row(
            Layout(name="tree", ratio=1),
            Layout(name="panels", ratio=2),
        )
        layout["tree"].update(Panel(_build_tree(model), title="steps"))
        layout["panels"].update(_build_panels_renderable(model))
        return layout

    # ------------------------------------------------------------------
    # Live display lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the Rich Live display."""
        self._live = Live(
            self._build_layout(self._model),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._live.start(refresh=True)

    def stop(self) -> None:
        """Stop the Rich Live display and restore the terminal."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def update(self, model: WatchModel) -> None:
        """Update the displayed model."""
        self._model = model
        if self._live is not None:
            self._live.update(self._build_layout(model))

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "WatchApp":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Plain line-log fallback (non-TTY / dumb terminal / non-UTF-8 locale)
# ---------------------------------------------------------------------------

class _PlainLineLog:
    """Minimal line-by-line printer used when the TUI cannot be shown.

    Each event produces one line on *stdout* of the form::

        [godel-watch] <op>  <key=value …>

    This class intentionally mirrors the ``WatchApp`` interface used in
    :func:`run_watch` so the main loop can treat both uniformly.
    """

    def __init__(self, *, file: IO | None = None) -> None:
        self._file = file or sys.stdout
        self._model = WatchModel.empty()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __enter__(self) -> "_PlainLineLog":
        return self

    def __exit__(self, *_) -> None:
        pass

    def print_event(self, event: dict) -> None:
        """Print a single event as a prefixed line."""
        op = event.get("op", "?")
        ts = event.get("ts", "")
        prefix = f"[godel-watch]"
        if ts:
            prefix += f" {ts[:19]}"
        prefix += f"  {op}"

        extras = []
        for key in ("step_path", "stream_path", "text", "line", "tool", "status"):
            val = event.get(key)
            if val:
                if isinstance(val, list):
                    val = "/".join(str(v) for v in val)
                extras.append(f"{key}={val!r}")
        if extras:
            suffix = "  " + "  ".join(extras)
        else:
            suffix = ""

        print(prefix + suffix, file=self._file, flush=True)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_terminal_restore_signals(app: WatchApp) -> list:
    """Install signal handlers that restore the terminal before default action.

    Returns the list of previous handlers (for cleanup / testing).
    """
    previous: list = []
    _signals = []
    if hasattr(signal, "SIGTSTP"):
        _signals.append(signal.SIGTSTP)
    if hasattr(signal, "SIGHUP"):
        _signals.append(signal.SIGHUP)

    def _handler(signum, frame, _app=app, _sig=None):
        _app.stop()
        # Restore default disposition and re-raise so the OS can handle it.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in _signals:
        try:
            prev = signal.signal(sig, _handler)
            previous.append((sig, prev))
        except (OSError, ValueError):
            # Ignore: signal cannot be caught in this thread (e.g. SIGTSTP on Windows)
            pass

    return previous


def _restore_signals(previous: list) -> None:
    """Restore previously saved signal handlers."""
    for sig, prev in previous:
        try:
            signal.signal(sig, prev)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Event queue draining + reduce pipeline
# ---------------------------------------------------------------------------

def _drain_queue(
    q: "queue.Queue[dict | None]",
    model: WatchModel,
    *,
    burst_threshold: int = _BURST_THRESHOLD,
) -> tuple[WatchModel, bool, bool]:
    """Drain *q* into *model* using burst coalescing.

    Drains at most *burst_threshold* events per call (or until the queue is
    empty / end-of-stream sentinel found).

    Returns
    -------
    (new_model, did_update, end_of_stream)
        * ``new_model`` — model after applying all drained events (may be the
          same object if nothing changed).
        * ``did_update`` — ``True`` if at least one state-changing event was
          applied.
        * ``end_of_stream`` — ``True`` if the ``None`` sentinel was found.
    """
    did_update = False
    end_of_stream = False
    drained = 0

    while drained < burst_threshold:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break

        if item is None:
            end_of_stream = True
            break

        old_model = model
        # TranscriptTail yields unwrapped inner event dicts (already stripped of
        # the {"event": ...} wrapper by _parse_lines).  Header lines are skipped
        # by TranscriptTail._parse_lines, so items here always have an "op" key.
        model = reduce(model, item)

        if model is not old_model:
            did_update = True

        drained += 1

    return model, did_update, end_of_stream


# ---------------------------------------------------------------------------
# Background producer thread
# ---------------------------------------------------------------------------

def _producer_thread(
    run_id: str,
    runs_dir: str,
    q: "queue.Queue[dict | None]",
) -> None:
    """Run in a daemon thread: read TranscriptTail events into *q*.

    Pushes raw dicts (parsed JSON lines) into *q*.  Pushes ``None`` as the
    end-of-stream sentinel when the tail reader is exhausted or errors.
    """
    import json
    from godel._tail import TranscriptTail, TranscriptTailError

    try:
        reader = TranscriptTail.from_run(run_id, runs_dir)
        for raw_line in reader:
            # TranscriptTail yields parsed event dicts
            q.put(raw_line if isinstance(raw_line, dict) else raw_line)
    except TranscriptTailError as exc:
        # Non-fatal: push sentinel so the main loop can exit cleanly.
        import logging
        logging.getLogger(__name__).warning("TranscriptTail error: %s", exc)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Producer thread error: %s", exc)
    finally:
        q.put(None)


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------

def _render_loop(
    renderer,  # WatchApp | _PlainLineLog
    q: "queue.Queue[dict | None]",
    plain: bool,
    *,
    timer_interval: float = _TIMER_INTERVAL,
    burst_threshold: int = _BURST_THRESHOLD,
) -> None:
    """Main loop: drain queue, coalesce bursts, update renderer.

    Uses a ``timer_interval``-second timer to trigger periodic renders even
    when the queue is quiet.
    """
    model = WatchModel.empty()
    last_render = time.monotonic()

    while True:
        model, did_update, end_of_stream = _drain_queue(
            q, model, burst_threshold=burst_threshold
        )

        now = time.monotonic()
        elapsed = now - last_render

        should_render = did_update and (
            elapsed >= timer_interval
            or q.qsize() == 0
        )

        if should_render:
            if plain:
                # For plain fallback, we rendered per-event in the producer.
                # Nothing extra to do here — model is tracked for completeness.
                pass
            else:
                renderer.update(model)
            last_render = now

        if end_of_stream:
            break

        # Avoid busy-spinning when queue is empty.
        if not did_update:
            time.sleep(timer_interval / 2)


# ---------------------------------------------------------------------------
# Plain-mode event loop (one line per event)
# ---------------------------------------------------------------------------

def _plain_loop(
    plain_log: _PlainLineLog,
    q: "queue.Queue[dict | None]",
    *,
    timer_interval: float = _TIMER_INTERVAL,
) -> None:
    """Event loop for the plain line-log fallback.

    Reads events from *q* and prints them one-by-one.
    """
    while True:
        try:
            item = q.get(timeout=timer_interval)
        except queue.Empty:
            continue

        if item is None:
            break

        if isinstance(item, dict):
            plain_log.print_event(item)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_watch(
    run_id: str,
    *,
    runs_dir: str = "./runs",
    stdout: IO | None = None,
    _burst_threshold: int = _BURST_THRESHOLD,
    _timer_interval: float = _TIMER_INTERVAL,
) -> None:
    """Start the live TUI (or plain line-log) for *run_id*.

    Parameters
    ----------
    run_id:
        The workflow run identifier.
    runs_dir:
        Directory containing per-run transcript directories.
    stdout:
        Output stream override (used by tests to capture output).
    _burst_threshold, _timer_interval:
        Coalescing knobs — exposed for testing only.
    """
    plain = _use_plain_fallback(stdout)

    event_q: queue.Queue[dict | None] = queue.Queue()

    # Start producer thread
    t = threading.Thread(
        target=_producer_thread,
        args=(run_id, runs_dir, event_q),
        daemon=True,
    )
    t.start()

    if plain:
        fh = stdout or sys.stdout
        plain_log = _PlainLineLog(file=fh)
        with plain_log:
            _plain_loop(plain_log, event_q, timer_interval=_timer_interval)
    else:
        console = Console(file=stdout or sys.stdout)
        app = WatchApp(run_id, console=console)
        previous_signals = _install_terminal_restore_signals(app)
        try:
            with app:
                _render_loop(
                    app,
                    event_q,
                    plain=False,
                    timer_interval=_timer_interval,
                    burst_threshold=_burst_threshold,
                )
        except KeyboardInterrupt:
            app.stop()
        finally:
            _restore_signals(previous_signals)

    t.join(timeout=2.0)

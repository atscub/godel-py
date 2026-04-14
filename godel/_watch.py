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
* The caller passes ``plain=True`` to :func:`run_watch` (CLI: ``--plain`` / ``-p``)
* The ``GODEL_WATCH_PLAIN`` environment variable is set to ``"1"``

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

# Internal sentinel op pushed by _producer_thread when the TranscriptTail
# iterator errors out (disk error, transcript disappeared, etc.).  Distinct
# from a clean EOS (None sentinel) so the consumer can surface an error banner
# instead of exiting silently.  Double-underscored to avoid colliding with any
# legitimate transcript op.
WATCH_ERROR_OP = "__watch_error__"


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
        """Start the Rich Live display.

        Idempotent: if the Live display is already running (``self._live`` is
        not ``None``), this method returns immediately without creating a
        second ``Live`` instance.  This prevents a double-call from orphaning
        the first ``Live`` context and corrupting terminal state.
        """
        if self._live is not None:
            return
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

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_ANSI = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "magenta": "\x1b[35m",
    "blue": "\x1b[34m",
    "red": "\x1b[31m",
    "reset": "\x1b[0m",
}


def _wrap_multiline(text: str, *, indent: str, width: int = 100) -> list[str]:
    """Split *text* into display lines, preserving existing newlines and
    soft-wrapping overlong lines.  Continuation lines are prefixed by *indent*."""
    out: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            out.append("")
            continue
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            if cut <= 0:
                cut = width
            out.append(raw[:cut])
            raw = indent + raw[cut:].lstrip()
        out.append(raw)
    return out


class _PlainLineLog:
    """Line-based renderer used when the Rich TUI cannot (or should not) run.

    Output is modeled after how ``claude`` itself renders a session in a
    terminal: one stanza per event, indented by step depth, with prompts,
    commands, tool calls and their results all shown inline so the reader can
    follow the conversation end-to-end.

    The class intentionally mirrors the :class:`WatchApp` lifecycle used in
    :func:`run_watch` so the main loop can treat both uniformly.
    """

    def __init__(self, *, file: IO | None = None) -> None:
        self._file = file or sys.stdout
        self._model = WatchModel.empty()
        self._use_color = bool(getattr(self._file, "isatty", lambda: False)())
        # (step_path, stream_path) keys we've already printed a header for.
        # Each parallel branch prints its header once; re-entering the branch
        # after interleaving with a sibling does not reprint.
        self._step_header_seen: set = set()
        # Spinner state: pending agent call awaiting a response on the same
        # stream_path.  Only animated on TTY output; no-op elsewhere.
        self._pending_stream: tuple | None = None
        self._pending_depth: int = 0
        self._spin_idx: int = 0
        self._spinner_active: bool = False
        # Whether any block has been emitted yet — used to gate the
        # separator blank line.
        self._emitted_any: bool = False
        # Most recently rendered response per stream_path.  Claude's
        # stream-json emits the final answer as both an assistant text block
        # and the terminating result event — both map to agent.response.
        # The record lets us drop the trailing duplicate while printing the
        # streamed answer in real time.
        self._last_response: dict[tuple, str] = {}
        # Active streaming burst per stream_path: op currently being appended
        # and its continuation indent.  Consecutive agent.response /
        # agent.thought chunks on the same stream render as one growing
        # block instead of one header-per-chunk.
        self._burst: dict[tuple, tuple[str, str]] = {}
        # Running concatenation of streamed text per stream_path — used to
        # dedupe the terminating full-text echo from ``_invoke``.
        self._stream_accum: dict[tuple, dict[str, str]] = {}
        # Visible column position per stream_path's in-progress line, used to
        # soft-wrap long streamed chunks so continuations stay aligned under
        # the block's indent instead of flushing to column zero.
        self._burst_col: dict[tuple, int] = {}
        # Branch correlation — parallel() branches are tagged "[b1]", "[b2]",
        # ... derived from the outermost stream_path element.  A small color
        # palette cycles so each branch also gets a stable color.  Sequential
        # (non-parallel) agent calls also get tags, which is harmless and
        # preserves consistent alignment.
        self._branch_index: dict[str, int] = {}
        # Muted 256-colour backgrounds paired with white foreground.  The bg
        # codes picked here are low-saturation / mid-luminance so the tag
        # reads cleanly on both light and dark terminal themes and doesn't
        # visually compete with the message text.
        self._branch_bg_escapes = (
            "\x1b[48;5;24m\x1b[97m",   # dark teal
            "\x1b[48;5;53m\x1b[97m",   # dark wine
            "\x1b[48;5;58m\x1b[97m",   # dark olive
            "\x1b[48;5;23m\x1b[97m",   # dark slate
            "\x1b[48;5;54m\x1b[97m",   # dark purple
        )

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __enter__(self) -> "_PlainLineLog":
        return self

    def __exit__(self, *_) -> None:
        pass

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _c(self, color: str, text: str) -> str:
        if not self._use_color:
            return text
        return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"

    # Visible width (characters) of the branch tag slot, including the
    # trailing space separator.  "[b1] " … "[b99] " all fit in 6 columns;
    # branches beyond b99 truncate rather than break alignment.
    _BRANCH_SLOT_WIDTH = 6

    def _branch_tag_raw(self, stream_path: tuple) -> str | None:
        """Return ``"b<n>"`` for *stream_path* or ``None`` if unbranched."""
        if not stream_path:
            return None
        root = stream_path[0]
        idx = self._branch_index.get(root)
        if idx is None:
            idx = len(self._branch_index) + 1
            self._branch_index[root] = idx
        return f"b{idx}"

    def _branch_prefix(self, stream_path: tuple) -> str:
        """Return the coloured branch badge (or padded blanks) for a line.

        Branched lines get a low-saturation background-tinted ``[bN]`` badge
        so branches form a vertically scannable column of tints; unbranched
        lines get a blank slot of the same width so alignment is preserved.
        """
        slot = self._BRANCH_SLOT_WIDTH
        tag = self._branch_tag_raw(stream_path)
        if tag is None:
            return " " * slot
        badge = f"[{tag}]"[: slot - 1]
        pad = " " * (slot - len(badge))
        if not self._use_color:
            return f"{badge}{pad}"
        esc = self._branch_bg_escapes[(self._branch_index[stream_path[0]] - 1) % len(self._branch_bg_escapes)]
        return f"{esc}{badge}{_ANSI['reset']}{pad}"

    def _emit(self, lines: list[str], stream_path: tuple = ()) -> None:
        prefix = self._branch_prefix(tuple(stream_path or ()))
        for line in lines:
            print(prefix + line, file=self._file, flush=True)
        if lines:
            self._emitted_any = True

    def _maybe_separator(self, op: str) -> None:
        """Insert a blank line between visual blocks.

        ``stdout`` and ``agent.raw`` are continuations of the preceding block
        (a run command's streaming output, or malformed JSONL in the middle
        of an agent's reply) so they skip the separator.
        """
        if not self._emitted_any:
            return
        if op in ("stdout", "agent.raw", "rotate"):
            return
        print("", file=self._file, flush=True)

    # ------------------------------------------------------------------
    # Spinner (thinking animation)
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Advance the spinner one frame.  Called from the event loop when
        the queue is idle and there's a pending agent call."""
        if not self._use_color or self._pending_stream is None:
            return
        frame = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
        self._spin_idx += 1
        pad = "  " * max(self._pending_depth, 1)
        msg = f"{pad}{_ANSI['dim']}{frame} thinking…{_ANSI['reset']}"
        try:
            self._file.write("\r\x1b[K" + msg)
            self._file.flush()
        except Exception:
            pass
        self._spinner_active = True

    def _append_chunk(self, cont: str, text: str, *, op: str, stream_path: tuple = (), _branch_prefix: str | None = None) -> None:
        """Write a streaming text chunk to the currently-open burst line.

        Embedded newlines are rewritten with the continuation indent so
        multi-paragraph output stays aligned.  Long lines are soft-wrapped at
        the terminal width with the same continuation indent so the reader
        doesn't see text flush to column zero mid-paragraph.  ``op`` picks the
        ANSI color (dim for thoughts, default for responses).
        """
        color = "dim" if op == "agent.thought" else None
        width = self._term_width()
        prefix = self._branch_prefix(tuple(stream_path or ())) if _branch_prefix is None else _branch_prefix
        # Wrap budget must account for the visible-width of the branch slot
        # (always ``_BRANCH_SLOT_WIDTH`` regardless of ANSI codes).
        line_start_width = self._BRANCH_SLOT_WIDTH + len(cont)
        col = self._burst_col.get(stream_path, line_start_width)
        parts = text.split("\n")

        def _write(segment: str) -> None:
            nonlocal col
            if not segment:
                return
            self._file.write(self._c(color, segment) if color else segment)
            col += len(segment)

        def _newline() -> None:
            nonlocal col
            self._file.write("\n" + prefix + cont)
            col = line_start_width

        for i, part in enumerate(parts):
            if i > 0:
                _newline()
            # Wrap an overlong part at the last space that fits on the
            # current line.  If no space fits, first try a fresh continuation
            # line; only hard-cut (mid-word) as a last resort.
            while part and width > len(cont) and col + len(part) > width:
                budget = width - col
                cut = part.rfind(" ", 0, budget + 1) if budget > 0 else -1
                if cut <= 0:
                    if col > len(cont):
                        # Move the whole word to a fresh line and retry.
                        _newline()
                        continue
                    cut = max(budget, 1)
                    head, rest = part[:cut], part[cut:]
                else:
                    head, rest = part[:cut], part[cut + 1:]
                _write(head)
                _newline()
                part = rest
            _write(part)

        self._burst_col[stream_path] = col
        try:
            self._file.flush()
        except Exception:
            pass
        self._emitted_any = True

    def _term_width(self) -> int:
        try:
            import shutil
            return max(shutil.get_terminal_size((100, 24)).columns, 20)
        except Exception:
            return 100

    def _end_burst(self, stream_path: tuple | None = None) -> None:
        """Close any active burst by emitting a trailing newline.

        If *stream_path* is given, only closes the burst for that stream;
        otherwise closes all bursts (used when the workflow ends).
        """
        if stream_path is None:
            if not self._burst:
                return
            self._file.write("\n")
            self._file.flush()
            self._burst.clear()
            self._burst_col.clear()
            return
        if stream_path in self._burst:
            self._file.write("\n")
            self._file.flush()
            del self._burst[stream_path]
            self._burst_col.pop(stream_path, None)

    def _clear_spinner(self) -> None:
        if self._spinner_active:
            try:
                self._file.write("\r\x1b[K")
                self._file.flush()
            except Exception:
                pass
            self._spinner_active = False

    def _step_header(self, step_path: list | tuple, stream_path: tuple = ()) -> None:
        sp = tuple(step_path or ())
        # Include stream_path in the dedup key so parallel branches of the
        # same step each get their own header — without it, two concurrent
        # invocations of ``ask_haiku`` would collapse under a single label.
        # Use only the outermost stream_path element as the branch
        # discriminator: agent.call and its nested run() emit events at
        # increasing stream_path depths (e.g. [branch], [branch, launch],
        # [branch, launch, run]), but they all belong to the same parallel
        # branch and should live under a single header.
        sp_root = tuple(stream_path or ())[:1]
        key = (sp, sp_root)
        if key in self._step_header_seen:
            return
        self._step_header_seen.add(key)
        if not sp:
            return
        indent = "  " * (len(sp) - 1)
        label = " / ".join(str(p) for p in sp)
        self._emit([f"{indent}{self._c('cyan', '▸ ' + label)}"], stream_path=stream_path)

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------

    def print_event(self, event: dict) -> None:
        op = event.get("op", "?")
        stream_path = tuple(event.get("stream_path") or [])

        # Dedupe the final full-text echo from ``_invoke``.  When streaming
        # is on we've already rendered the answer as chunks; the accumulated
        # text lives in ``_stream_accum``.  The post-call agent.response
        # event carries the same full text — drop it.
        if op == "agent.response":
            accum = (self._stream_accum.get(stream_path) or {}).get("agent.response", "")
            text = event.get("text") or ""
            if accum and text.strip() == accum.strip():
                # The streamed burst is complete — close it so the next
                # event starts with a proper separator, not glued to the
                # trailing chunk.
                self._end_burst(stream_path)
                return

        # Thinking blocks are never rendered — the spinner animation conveys
        # "thinking" instead.  Swallowing here (before _render) keeps the
        # spinner running and avoids inserting a separator for a no-op event.
        if op == "agent.thought":
            return

        self._render(event)

        if op == "agent.prompt":
            self._last_response.pop(stream_path, None)
            self._stream_accum.pop(stream_path, None)

    def _render(self, event: dict) -> None:
        op = event.get("op", "?")
        step_path = event.get("step_path") or []
        stream_path = tuple(event.get("stream_path") or [])
        depth = len(step_path)
        pad = "  " * max(depth, 1)
        cont = pad + "    "  # continuation indent for multi-line text

        # If this event continues the currently-active burst on this stream
        # (e.g. another agent.response chunk right after the previous one),
        # append directly to the in-progress line instead of re-emitting a
        # header or a blank separator.
        cur_burst = self._burst.get(stream_path)
        if cur_burst is not None and cur_burst[0] == op and op in ("agent.response", "agent.thought"):
            text = event.get("text", "")
            if text:
                self._append_chunk(cur_burst[1], text, op=op, stream_path=stream_path)
                accum = self._stream_accum.setdefault(stream_path, {})
                accum[op] = accum.get(op, "") + text
            return

        # Any visible output must first wipe any in-progress spinner line
        # and terminate any open burst.
        self._end_burst(stream_path)
        self._clear_spinner()

        # An event on the pending agent's stream means the response (or a
        # tool call / thought) is arriving — stop the spinner.
        if self._pending_stream is not None and stream_path == self._pending_stream and op != "agent.prompt":
            self._pending_stream = None

        self._maybe_separator(op)

        # Group events by step for readability.
        if op not in ("rotate", "WORKFLOW_FINISHED"):
            self._step_header(step_path, stream_path)

        if op == "agent.prompt":
            prompt = event.get("prompt", "")
            model = event.get("model", "")
            header = pad + self._c("magenta", f"› prompt") + (f" {self._c('dim', '(' + model + ')')}" if model else "")
            self._emit([header] + [cont + ln for ln in _wrap_multiline(prompt, indent="", width=max(self._term_width() - len(cont) - self._BRANCH_SLOT_WIDTH, 20))], stream_path=stream_path)
            # Arm the spinner for the forthcoming response.
            self._pending_stream = stream_path
            self._pending_depth = depth
            self._spin_idx = 0
            return

        if op == "agent.response":
            text = event.get("text", "")
            self._emit([pad + self._c("green", "‹ response")], stream_path=stream_path)
            branch_prefix = self._branch_prefix(stream_path)
            self._file.write(branch_prefix + cont)
            self._burst_col[stream_path] = self._BRANCH_SLOT_WIDTH + len(cont)
            if text:
                self._append_chunk(cont, text, op=op, stream_path=stream_path, _branch_prefix=branch_prefix)
            self._burst[stream_path] = (op, cont)
            accum = self._stream_accum.setdefault(stream_path, {})
            accum[op] = accum.get(op, "") + text
            return


        if op == "agent.tool_call":
            tool = event.get("tool", "?")
            inp = event.get("input")
            head = pad + self._c("yellow", f"⚒ {tool}")
            body: list[str] = [head]
            if inp is not None:
                import json as _json
                try:
                    rendered = _json.dumps(inp, indent=2, ensure_ascii=False)
                except Exception:
                    rendered = repr(inp)
                for ln in rendered.split("\n"):
                    body.append(cont + ln)
            self._emit(body, stream_path=stream_path)
            return

        if op == "agent.tool_result":
            tool = event.get("tool", "?")
            out = event.get("output")
            if isinstance(out, (dict, list)):
                import json as _json
                try:
                    text = _json.dumps(out, indent=2, ensure_ascii=False)
                except Exception:
                    text = repr(out)
            else:
                text = "" if out is None else str(out)
            lines = text.split("\n")
            shown = lines[:8]
            more = len(lines) - len(shown)
            body = [pad + self._c("dim", f"↳ {tool} result")]
            body += [cont + ln for ln in shown]
            if more > 0:
                body.append(cont + self._c("dim", f"… (+{more} more lines)"))
            self._emit(body, stream_path=stream_path)
            return

        if op == "agent.raw":
            text = event.get("text", "")
            reason = event.get("reason", "")
            head = pad + self._c("red", "! raw") + (f" {self._c('dim', '(' + reason + ')')}" if reason else "")
            self._emit([head] + [cont + ln for ln in _wrap_multiline(text, indent="", width=max(self._term_width() - len(cont) - self._BRANCH_SLOT_WIDTH, 20))], stream_path=stream_path)
            return

        if op == "run.start":
            # Suppress nested run.start: when stream_path depth > 1 the
            # command was launched by an agent (or another run) whose own
            # event already surfaces the semantic input.  Showing the raw
            # `claude -p ...` shell invocation next to the agent.prompt is
            # redundant noise.
            if len(stream_path) > 1:
                return
            cmd = event.get("cmd", "")
            self._emit(
                [pad + self._c("yellow", "$ ") + cmd.split("\n", 1)[0][:200]]
                + ([cont + ln for ln in cmd.split("\n")[1:]] if "\n" in cmd else []),
                stream_path=stream_path,
            )
            return

        if op == "stdout":
            line = event.get("line", "")
            self._emit([pad + self._c("dim", "│ ") + line], stream_path=stream_path)
            return

        if op == "print":
            text = event.get("text", "")
            self._emit(
                [pad + self._c("bold", "» ") + (text.split("\n", 1)[0] if text else "")]
                + [cont + ln for ln in text.split("\n")[1:]],
                stream_path=stream_path,
            )
            return

        if op == "WORKFLOW_FINISHED":
            self._end_burst()
            status = event.get("status", "FINISHED")
            color = "green" if status == "FINISHED" else "red"
            self._emit([self._c(color, f"── workflow {status.lower()} ──")], stream_path=stream_path)
            return

        # Fallback for any other / future op: compact key=val line.
        extras = []
        for key in ("stream_path", "text", "line", "tool", "status"):
            val = event.get(key)
            if val:
                if isinstance(val, list):
                    val = "/".join(str(v) for v in val)
                extras.append(f"{key}={val!r}")
        suffix = ("  " + "  ".join(extras)) if extras else ""
        self._emit([pad + self._c("dim", f"· {op}") + suffix], stream_path=stream_path)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_terminal_restore_signals(
    app: WatchApp,
    pending_signal: list,
) -> list:
    """Install signal handlers that flag a pending signal for the main loop.

    The handler is **async-signal-safe**: it only sets a flag (``pending_signal``
    is a single-element list used as a mutable cell) and does not touch any
    Rich state.  The main render loop observes the flag, calls
    :meth:`WatchApp.stop` under normal control flow, restores the default
    disposition, and re-raises the signal for the OS to handle.

    This avoids the deadlock risk of calling ``Live.stop()`` directly from
    a signal handler when Rich is mid-render and holds its internal lock.

    Parameters
    ----------
    app:
        The ``WatchApp`` to stop on signal (unused directly by the handler;
        kept for parity with the call site and future extensions).
    pending_signal:
        A single-element mutable list.  The handler writes the signal number
        into ``pending_signal[0]``; the caller observes it.

    Returns
    -------
    list
        Pairs of ``(signum, previous_handler)`` for cleanup.
    """
    previous: list = []
    _signals = []
    if hasattr(signal, "SIGTSTP"):
        _signals.append(signal.SIGTSTP)
    if hasattr(signal, "SIGHUP"):
        _signals.append(signal.SIGHUP)

    def _handler(signum, frame):
        # Async-signal-safe: only write a Python int into a pre-allocated list
        # slot.  Do NOT call Rich / Live.stop() here — that would risk deadlock
        # if the signal lands while Rich holds its internal lock.
        pending_signal[0] = signum

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

    Note: if a ``WATCH_ERROR_OP`` sentinel is drained, the error dict is
    captured in module-level ``_last_producer_error`` so the render loop can
    surface it after the loop exits.
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

        # Capture producer-side error sentinels so the render loop can surface
        # them.  The error dict is not applied to the model (reduce treats
        # unknown ops as a no-op) but we stash it globally for the main thread
        # to print after EOS.
        if isinstance(item, dict) and item.get("op") == WATCH_ERROR_OP:
            global _last_producer_error
            _last_producer_error = item
            drained += 1
            continue

        old_model = model
        # TranscriptTail yields unwrapped inner event dicts (already stripped of
        # the {"event": ...} wrapper by _parse_lines).  Header lines are skipped
        # by TranscriptTail._parse_lines, so items here always have an "op" key.
        model = reduce(model, item)

        if model is not old_model:
            did_update = True

        drained += 1

    return model, did_update, end_of_stream


# Module-level cell for producer error; consulted by _render_loop / run_watch
# after EOS to surface a banner.  It is reset at the start of each run_watch
# invocation and is read/written only from the main thread inside _drain_queue
# plus run_watch itself, so no lock is needed.
_last_producer_error: dict | None = None


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

    Back-pressure policy: if *q* is bounded (``maxsize > 0``) and full, the
    **oldest** queued event is dropped to make room for the new one.  This
    preserves the most-recent run state at the cost of lost scrollback.
    A single warning is logged per producer-thread session.
    """
    import logging
    from godel._tail import TranscriptTail, TranscriptTailError

    logger_ = logging.getLogger(__name__)
    overflow_warned = False

    def _put_with_drop_oldest(item):
        nonlocal overflow_warned
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            if not overflow_warned:
                logger_.warning(
                    "watch event queue full (maxsize=%s); dropping oldest "
                    "events to keep pace with producer",
                    q.maxsize,
                )
                overflow_warned = True
            # Drop oldest, then append.  Races with the consumer are benign:
            # if the consumer drains concurrently, put_nowait may now succeed
            # immediately; worst case we drop one extra oldest item.
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                # Give up silently — consumer is making progress, next put
                # will likely succeed.
                pass

    error_event: dict | None = None
    try:
        reader = TranscriptTail.from_run(run_id, runs_dir)
        for event in reader:
            _put_with_drop_oldest(event)
            # WORKFLOW_FINISHED is the terminal sentinel emitted by TranscriptWriter
            # just before close().  When we see it the run is complete — push EOS
            # immediately so the render loop exits cleanly without waiting for the
            # next polling interval.
            if event.get("op") == "WORKFLOW_FINISHED":
                return  # finally block below pushes None sentinel
    except TranscriptTailError as exc:
        logger_.warning("TranscriptTail error: %s", exc)
        error_event = {
            "op": WATCH_ERROR_OP,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    except Exception as exc:
        logger_.warning("Producer thread error: %s", exc)
        error_event = {
            "op": WATCH_ERROR_OP,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        if error_event is not None:
            # Push the error sentinel BEFORE the EOS None so the consumer can
            # distinguish clean EOS from error EOS and surface an error banner.
            try:
                _put_with_drop_oldest(error_event)
            except Exception:
                pass
        # Sentinel must always get through — use blocking put so we never
        # strand the main loop waiting for EOS.
        try:
            q.put(None, timeout=1.0)
        except queue.Full:
            # Queue is full AND consumer is not draining — best effort: drop
            # oldest and retry once.
            try:
                q.get_nowait()
                q.put_nowait(None)
            except (queue.Empty, queue.Full):
                # Currently unreachable under the single-producer invariant
                # (this finally block is the only code path that puts the
                # sentinel, and the consumer drains strictly faster than this
                # tight drop-oldest + put_nowait pair).  Log loudly rather
                # than swallowing silently so any future regression — e.g.
                # adding a second producer, or a consumer stall during
                # teardown — surfaces as a visible error instead of a hang.
                logger_.error(
                    "watch producer: failed to enqueue end-of-stream sentinel "
                    "after drop-oldest retry (queue maxsize=%s). Render loop "
                    "may hang waiting for EOS.",
                    q.maxsize,
                )


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------

def _render_loop(
    renderer,  # WatchApp
    q: "queue.Queue[dict | None]",
    *,
    timer_interval: float = _TIMER_INTERVAL,
    burst_threshold: int = _BURST_THRESHOLD,
    pending_signal: list | None = None,
) -> None:
    """Main loop: drain queue, coalesce bursts, update renderer.

    Uses a ``timer_interval``-second timer to trigger periodic renders even
    when the queue is quiet.  Guarantees a **final flush render** before
    exiting on end-of-stream so that fully-completed runs still display their
    final state.

    Parameters
    ----------
    renderer:
        An object with an ``update(model)`` method (typically a ``WatchApp``).
    q:
        Queue producing event dicts; ``None`` sentinel signals end-of-stream.
    timer_interval, burst_threshold:
        Coalescing knobs.
    pending_signal:
        Optional single-element list used by the signal handler to communicate
        a pending SIGTSTP/SIGHUP.  When set, the loop stops the renderer,
        restores default disposition, and re-raises the signal.
    """
    model = WatchModel.empty()
    last_render = time.monotonic()
    any_update_since_last_render = False

    while True:
        # Check for a pending signal before doing work — handler-to-loop
        # handoff, safe to call Rich here (no signal context).
        if pending_signal is not None and pending_signal[0] is not None:
            signum = pending_signal[0]
            try:
                renderer.stop()
            finally:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
            return

        model, did_update, end_of_stream = _drain_queue(
            q, model, burst_threshold=burst_threshold
        )

        if did_update:
            any_update_since_last_render = True

        now = time.monotonic()
        elapsed = now - last_render

        # Render when the timer elapsed AND we have something new.  The
        # previous `q.qsize() == 0` branch was unreliable because the ``None``
        # sentinel sits in the queue during the last real-event drain — that
        # masked fast-run progress entirely.
        should_render = any_update_since_last_render and elapsed >= timer_interval

        if should_render:
            renderer.update(model)
            last_render = now
            any_update_since_last_render = False

        if end_of_stream:
            # Re-check the signal flag before we commit to the EOS exit path.
            # A SIGTSTP/SIGHUP that lands in the tail window (between the
            # top-of-loop check and here) would otherwise be silently
            # consumed, leaving the user's Ctrl+Z / terminal-drop ignored.
            # Handle it via the same stop → SIG_DFL → re-raise dance.
            if pending_signal is not None and pending_signal[0] is not None:
                signum = pending_signal[0]
                try:
                    renderer.stop()
                finally:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)
                return

            # Guarantee a final flush render if any events were applied since
            # the last paint.  Fixes the "0 renders on fast run" bug.
            if any_update_since_last_render:
                renderer.update(model)
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

    Reads events from *q* and prints them one-by-one.  Idle ticks drive the
    thinking-spinner so prompts in flight animate while awaiting a response.
    """
    # Tick the spinner roughly every 100ms regardless of event arrival rate.
    spinner_interval = 0.1
    while True:
        try:
            item = q.get(timeout=spinner_interval)
        except queue.Empty:
            plain_log.tick()
            continue

        if item is None:
            break

        if isinstance(item, dict):
            if item.get("op") == WATCH_ERROR_OP:
                # Capture the producer error for the outer printer; skip rendering.
                global _last_producer_error
                _last_producer_error = item
                continue
            plain_log.print_event(item)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Default bound on the event queue.  A fast producer (e.g. a replay burst)
# should not be able to exhaust memory if the renderer stalls.  On overflow
# ``_producer_thread`` drops the *oldest* event and logs a warning, which
# preserves the most-recent run state at the cost of lost scrollback.
_EVENT_QUEUE_MAXSIZE = 10_000


def run_watch(
    run_id: str,
    *,
    runs_dir: str = "./runs",
    plain: bool = False,
    stdout: IO | None = None,
    _burst_threshold: int = _BURST_THRESHOLD,
    _timer_interval: float = _TIMER_INTERVAL,
    _queue_maxsize: int = _EVENT_QUEUE_MAXSIZE,
) -> None:
    """Start the live TUI (or plain line-log) for *run_id*.

    Parameters
    ----------
    run_id:
        The workflow run identifier.
    runs_dir:
        Directory containing per-run transcript directories.
    plain:
        Force plain line-log output even on a capable TTY.  Equivalent to
        setting ``GODEL_WATCH_PLAIN=1`` in the environment.
    stdout:
        Output stream override (used by tests to capture output).
    _burst_threshold, _timer_interval, _queue_maxsize:
        Coalescing / back-pressure knobs — exposed for testing only.
    """
    plain = plain or _use_plain_fallback(stdout) or os.environ.get("GODEL_WATCH_PLAIN") == "1"

    # Reset the producer-error cell at the start of every invocation so that
    # stale errors from a prior run do not leak into this one.
    global _last_producer_error
    _last_producer_error = None

    event_q: queue.Queue[dict | None] = queue.Queue(maxsize=_queue_maxsize)

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
        # Single-element mutable cell shared with the signal handler.  The
        # handler writes the received signum; the render loop observes it.
        pending_signal: list = [None]
        previous_signals = _install_terminal_restore_signals(app, pending_signal)
        try:
            with app:
                _render_loop(
                    app,
                    event_q,
                    timer_interval=_timer_interval,
                    burst_threshold=_burst_threshold,
                    pending_signal=pending_signal,
                )
        finally:
            _restore_signals(previous_signals)

    t.join(timeout=2.0)

    # If the producer thread errored mid-run, surface a banner on stderr so
    # the user sees the failure (instead of the TUI silently closing).  This
    # runs AFTER the live display has stopped so it is not overdrawn.
    if _last_producer_error is not None:
        err_type = _last_producer_error.get("error_type", "Error")
        err_msg = _last_producer_error.get("error", "unknown")
        print(
            f"[godel-watch] transcript error ({err_type}): {err_msg}",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------

_STREAM_AGENTS_HINT = (
    "agent streaming was disabled for this run (--no-stream); "
    "re-run without --no-stream to enable live streaming. "
    "See docs/transcript-format.md"
)


def _transcript_dir_exists(run_id: str, runs_dir: str) -> bool:
    """Return True if the transcript directory for *run_id* exists.

    A transcript directory is created when agent streaming is enabled
    (the default; disabled only via ``godel run --no-stream``).  Its
    absence indicates streaming was disabled for this run.
    """
    from pathlib import Path
    return (Path(runs_dir) / run_id).exists()


if __name__ == "__main__":
    """Subprocess entry point: ``python -m godel._watch <run_id> [--runs-dir DIR]``

    This is the isolation boundary between the renderer and the workflow
    process.  A renderer crash (e.g. Rich internal error, SIGKILL) cannot
    propagate to the underlying run.

    Exit codes
    ----------
    0  — normal exit (EOS received or Ctrl+C)
    1  — missing / ambiguous run_id
    2  — watch not installed (rich missing)
    """
    import argparse

    ap = argparse.ArgumentParser(prog="python -m godel._watch", add_help=False)
    ap.add_argument("run_id")
    ap.add_argument("--runs-dir", default="./runs")
    ap.add_argument(
        "--hint-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait before showing the streaming-disabled hint",
    )
    ap.add_argument(
        "--plain",
        action="store_true",
        default=False,
        help="Force plain line-log output instead of the Rich TUI.",
    )
    ns = ap.parse_args()

    # Discoverability hint: if the transcript directory does not exist within
    # --hint-timeout seconds, streaming is likely disabled.  Show the hint on
    # stderr and exit so we don't hang indefinitely.
    import time as _time
    _deadline = _time.monotonic() + ns.hint_timeout
    while not _transcript_dir_exists(ns.run_id, ns.runs_dir):
        if _time.monotonic() >= _deadline:
            print(
                f"[godel-watch] hint: {_STREAM_AGENTS_HINT}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(0)
        _time.sleep(0.1)

    try:
        run_watch(ns.run_id, runs_dir=ns.runs_dir, plain=ns.plain)
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[godel-watch] error: {exc}", file=sys.stderr)
        sys.exit(2)

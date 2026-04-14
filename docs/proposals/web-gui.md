# Web GUI for `godel watch` — Scoping Proposal

**Status:** DRAFT — for decision, not implementation.
**Author:** Agent session (godel-py-5wk)
**Date:** 2026-04-14

---

## 1. Problem Statement

### Why the current Rich TUI is hitting limits

`godel/_watch.py` implements a Rich-based live TUI that renders step status and agent stream output in a split-panel layout. After dogfooding across real workflow runs, three categories of friction have emerged:

**Panel bloat with no expand/collapse.** `_build_panels_renderable()` stacks up to 3 active `StreamPanel` boxes inline and collapses overflow into a `"+N more"` strip showing only the last 60 characters of each panel's most recent line. When a step runs a multi-turn Claude session, the panel fills with truncated tool call payloads. There is no way to expand a panel to full content without exiting the TUI and inspecting the raw transcript JSONL manually.

**No diff/JSON/markdown rendering.** `_event_to_line()` in `_watch_model.py` converts `agent.tool_call` events to a flat string `[tool_call] <tool>: <input>` and `agent.tool_result` to `[tool_result] <tool>: <output>`. In practice, tool inputs and outputs are JSON objects (often multi-hundred-line diff outputs from git tools). The terminal renders these as escaped single-line strings — unreadable at a glance.

**Single-run focus, no history.** The TUI attaches to one `run_id` and exits. There is no way to navigate between past runs, search for a particular step output across runs, or open two runs side-by-side for comparison.

**Layout rigidity.** The `Layout` in `WatchApp._build_layout()` splits horizontally: 1/3 tree, 2/3 panels. On narrow terminals the panels column is unusably thin; on wide monitors it wastes space. Terminal layout cannot adapt to content the way a browser flexbox can.

**SSH / CI path is already degraded.** On non-TTY environments, `_use_plain_fallback()` already falls back to `_PlainLineLog`: a simple prefixed line printer. This means there are already two rendering paths and the Rich path only activates when a TTY with UTF-8 is present.

### Specific friction observed in real runs

- Reviewing a 40-step research workflow: had to kill the TUI and `grep` the transcript JSONL to find where a specific tool call failed — the panel showed only the last line of a 200-line JSON output.
- Running `godel run workflow.py --watch` in a tmux split: panel redraws caused screen flicker every 100 ms even when nothing was happening.
- Post-run replay (`godel watch <completed-run-id>`): replays so fast that useful intermediate states are invisible — the TUI snaps directly to the final state.

---

## 2. Goals and Non-Goals

### Goals (v1)

- **Live observation of in-flight runs.** Connect to a running workflow and see step status and agent stream output update in near-real-time (≤500 ms latency).
- **Deep inspection of full tool I/O.** Click to expand any `agent.tool_call` / `agent.tool_result` event and see the complete payload with syntax highlighting (JSON, diff, markdown).
- **Searchable history of past runs.** List all runs in `./runs/`, click to open a replay view. Full-text search across a run's transcript.
- **Shareable localhost URL for pair debugging.** `http://localhost:<port>/<run_id>` is a stable URL a teammate can open while both are on the same machine (or via SSH port-forward).

### Non-Goals (v1)

- Authentication beyond localhost binding. No tokens, no TLS, no user accounts.
- Remote or multi-user deployment. The server is not a daemon; it exits when the `godel watch --web` process exits.
- Editing or replay initiation from the UI. Read-only observability only.
- Mobile-responsive layout. Desktop-browser viewport assumed.
- Replacing the plain line-log fallback. `_PlainLineLog` survives as the SSH/CI mode.
- Real-time collaboration (shared cursors, live annotations).

---

## 3. Architecture

### 3.1 Server

**Framework:** FastAPI (already in the Python ecosystem orbit; async-native; SSE support via `sse-starlette`).

**Process model:** `godel watch --web [--port 0]` spawns a `uvicorn` instance in the same process (or a lightweight subprocess). The server is not a daemon — it lives for the lifetime of the `godel watch --web` invocation and exits with it. This is identical to how Jupyter, MLflow, and Ray Dashboard distribute their UIs.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the bundled SPA index.html |
| `GET` | `/assets/*` | Static bundled JS/CSS |
| `GET` | `/api/runs` | List run IDs and their metadata (scans `./runs/`) |
| `GET` | `/api/runs/{run_id}/events` | SSE stream: tail `runs/<run_id>/transcript.jsonl` and broadcast parsed events |
| `GET` | `/api/runs/{run_id}/events?from=0` | Replay from offset `from` then stream live |

**Server-side state:** Zero persistent state. The server holds file watchers (one per active SSE subscriber) but no in-memory model. The `WatchModel` reducer lives in the **client** (or is computed server-side and shipped as deltas — see 3.3).

**Dependency:** `fastapi`, `uvicorn[standard]`, `sse-starlette`. These would be added as a new optional group `godel[web]`.

### 3.2 Frontend

**Option A — Vite + React + Tailwind (recommended):**
- Familiar to most contributors; rich component ecosystem.
- `@tanstack/react-query` for SSE state management.
- Builds to a single `dist/` bundle that is included in the Python wheel under `godel/web/dist/`.
- Build step is a `make web` / `npm run build` command in the repo root; CI runs it and commits the artifact (or builds at release time via a separate workflow step).
- Total bundle size estimate: ~180 KB gzip (React 18 + Tailwind purged + SSE client).

**Option B — HTMX + minimal vanilla JS:**
- No build pipeline; the server renders HTML with `Jinja2` templates; HTMX handles SSE and partial updates.
- Dramatically less frontend surface area (~1 200 LOC JS vs ~8 000 LOC TS).
- Trade-off: syntax highlighting (Prism.js / Shiki) and collapsible tree UI are harder to compose correctly in server-side templates. Contributor familiarity is lower.

**Recommendation:** Option A (React). The UI needs interactive expand/collapse, a collapsible step tree, and a JSON/diff viewer — these are composable React components that already exist (`react-json-view`, `react-syntax-highlighter`). HTMX would require reimplementing this interaction layer from scratch.

### 3.3 Data Flow

```
transcript.jsonl  ──read──►  FastAPI SSE endpoint  ──push──►  Browser EventSource
                                    │
                              (server-side: raw events as JSON lines)
                                    │
                              Browser: runs reduce(model, event) on each line
                              (same WatchModel shape; reducer ported to TypeScript)
```

Two viable approaches for the reducer:

**Approach 1 — Client-side reducer (recommended):**
Port `reduce()` and `reduce_header()` from `godel/_watch_model.py` to TypeScript. The server ships raw event lines verbatim; the browser maintains the accumulated `WatchModel`. This keeps the server stateless and means the frontend state is always consistent with the transcript — no delta-sync protocol needed.

**Approach 2 — Server-side reducer + delta protocol:**
Server maintains a `WatchModel` per subscribed run and ships `{type: "patch", ops: [...]}` JSON Patch diffs. Avoids porting the reducer to TS but adds a stateful server layer and a delta protocol to maintain. Drift risk is higher.

**Recommended:** Approach 1. The `reduce()` function is 130 lines of pure dataclass logic with no Python-specific dependencies. A TypeScript port is straightforward and can be round-trip tested against the Python implementation using shared transcript fixtures.

### 3.4 Distribution

- `godel[web]` optional dependency group adds `fastapi`, `uvicorn[standard]`, `sse-starlette`.
- Bundled frontend lives at `godel/web/dist/` (checked into the repo as a build artifact, rebuilt on release).
- `godel watch --web [--port 0]` spawns server, opens browser (`webbrowser.open()`), and blocks until Ctrl+C (same lifecycle as `godel watch`).
- `godel ui` as an alias for `godel watch --web` (no run_id required — shows run list).
- `--port 0` tells the OS to assign a free port (avoids conflict with other localhost services).
- No daemon, no background service, no config file.

---

## 4. What Survives, What's Deleted

### Survives

| Artifact | Why |
|----------|-----|
| `godel/_watch_model.py` (285 LOC) | Pure frozen-dataclass model + `reduce()` / `reduce_header()`. Zero rendering logic. Reused unchanged by any renderer. |
| `godel/_watch_model.py`: `WatchModel`, `StepNode`, `StreamPanel`, `reduce`, `reduce_header` | Source of truth for the observable state shape. |
| `godel/_watch.py`: `_PlainLineLog` (lines 319–368, ~50 LOC) | SSH/CI plain line-log; survives as the non-web, non-TTY fallback. |
| `godel/_watch.py`: `_use_plain_fallback()` (lines 80–100, ~21 LOC) | Fallback detection logic; still needed to decide between plain and web mode. |
| `godel/_watch.py`: `_producer_thread()` (lines 513–618, ~106 LOC) | Transcript-reading background thread with back-pressure. Reused by the SSE endpoint (or replaced by an equivalent async reader built on `TranscriptTail`). |
| `godel/_tail.py` (986 LOC) | `TranscriptTail` rotation-chain reader. The SSE server uses this directly. |
| Transcript JSONL format | Protocol is stable; the web frontend consumes the same events. |
| `runs/<id>/transcript.jsonl` rotation logic | Unchanged. |

### Deleted (on full pivot) or Deprecated (on hybrid)

| Artifact | LOC | Reason |
|----------|-----|--------|
| `godel/_watch.py`: `WatchApp` class (lines 218–313, ~96 LOC) | ~96 | Rich panel layout; replaced by browser UI |
| `godel/_watch.py`: `_build_tree()` (lines 137–162, ~26 LOC) | ~26 | Rich tree renderer; replaced by React tree component |
| `godel/_watch.py`: `_build_panels_renderable()` (lines 169–211, ~43 LOC) | ~43 | Rich panel stacker; replaced by React panels |
| `godel/_watch.py`: `_step_label()` (lines 122–134, ~13 LOC) | ~13 | Rich Text label builder; replaced by React component |
| `godel/_watch.py`: `_panel_title()` (lines 165–166, ~2 LOC) | ~2 | Trivial helper; inlined or removed |
| `godel/_watch.py`: `_STATUS_STYLE`, `_STATUS_ICON` dicts (lines 107–119, ~12 LOC) | ~12 | Terminal colour/icon tables; replaced by CSS |
| `godel/_watch.py`: `_render_loop()` (lines 624–713, ~90 LOC) | ~90 | Rich-specific render loop with timer coalescing; SSE push replaces polling |
| `godel/_watch.py`: `_install_terminal_restore_signals()` + `_restore_signals()` (lines 374–432, ~59 LOC) | ~59 | Terminal signal handling for Rich Live; not needed in web server |
| `godel/_watch.py`: `_drain_queue()` (lines 440–499, ~60 LOC) | ~60 | Queue drainer for render coalescing; SSE uses async iterator directly |
| `rich>=13.0,<15` dependency in `pyproject.toml` `[watch]` group | — | Removed; `rich` is no longer needed |

**Net LOC change estimate (full pivot):**

| Category | LOC |
|----------|-----|
| Deleted from `godel/_watch.py` | −501 |
| Kept in `godel/_watch.py` (plain log + fallback + producer thread) | +177 |
| New `godel/_web.py` (FastAPI server + SSE endpoint) | +~350 |
| New TypeScript reducer port (`src/watchModel.ts`) | +~200 |
| New React UI components | +~600 |
| **Net change** | **+~826 LOC total** (Python: −151 net, TS: +800) |

**Hybrid variant:** `WatchApp`, `_render_loop`, signal handlers, and related functions are kept as-is (−0 LOC from `_watch.py`). The web server and frontend are added on top. Net addition: +~1 150 LOC.

---

## 5. UX Sketch

### Main run view (`/runs/<run_id>`)

```
┌─────────────────────────────────────────────────────────────────────┐
│  godel watch  ·  run_id: 01J8K...   ●  running   (42s elapsed)      │
├──────────────────┬──────────────────────────────────────────────────┤
│  Steps           │  Streams                                          │
│                  │  ┌──────────────────────────────────────────┐    │
│  ✓ fetch_data    │  │  agent/claude  [running]              ▼  │    │
│  ⏳ analyse       │  │  Thought: I need to check the diff...     │    │
│    ⏳ call_api    │  │  tool_call: Read {"path": "src/foo.py"}   │    │
│    ○ summarise   │  │  tool_result: [click to expand 847 chars] │    │
│  ○ report        │  └──────────────────────────────────────────┘    │
│                  │  ┌──────────────────────────────────────────┐    │
│                  │  │  stdout/analyse  [running]            ▼  │    │
│                  │  │  Analysing 12 files...                    │    │
│                  │  └──────────────────────────────────────────┘    │
│                  ├──────────────────────────────────────────────────┤
│                  │  Selected event detail                            │
│                  │  ─────────────────────────────────────────────   │
│                  │  op: agent.tool_result                            │
│                  │  tool: Read                                       │
│                  │  ts: 2026-04-14T10:23:01Z                        │
│                  │  output: (syntax-highlighted JSON / diff)         │
│                  │    {                                              │
│                  │      "content": "- old line\n+ new line\n..."    │
│                  │    }                                              │
└──────────────────┴──────────────────────────────────────────────────┘
```

Key interactions:
- Click any tool call line → selected event detail panel shows full payload with syntax highlighting.
- `▼` toggle on each stream panel → collapse/expand stream content.
- Step tree nodes are clickable: clicking a step filters the stream panels to events from that step's `stream_path`.

### History view (`/`)

```
┌─────────────────────────────────────────────────────────────────────┐
│  godel ui                                            Search: [     ] │
├─────────────────────────────────────────────────────────────────────┤
│  Run ID             Started              Status   Steps  Duration    │
│  01J8K3M...         2026-04-14 10:20     ✓ done   12     1m 4s       │
│  01J8K0N...         2026-04-14 09:48     ✗ failed  7     23s         │
│  01J8JR4...         2026-04-14 09:12     ✓ done   20     3m 12s      │
└─────────────────────────────────────────────────────────────────────┘
```

Clicking a row opens that run in the main run view.

---

## 6. Implementation Phasing

### Phase A — Server-only (SSE firehose)

**Deliverable:** `godel watch --web` spawns a FastAPI server. Frontend is a single `web.html` file served inline (no build pipeline, no npm). Raw events arrive as SSE and are appended to a `<pre>` log — effectively a browser-native version of `_PlainLineLog`. `_PlainLineLog` is kept for SSH/CI.

**Replaces:** `godel watch --plain` (which does not currently exist as a flag; this is a new entry point for browser users).

**LOC:** ~200 Python (FastAPI + SSE + static HTML) + ~100 vanilla JS.

**Value:** Any run can be observed in a browser tab. Events stream in real time. Full event payloads are visible without truncation (just scroll the raw log). Shareable URL works immediately.

**Does not require:** npm, TypeScript, bundler, build step.

### Phase B — Structured panels (TUI feature parity)

**Deliverable:** Replace the inline `web.html` with a proper React SPA. Step tree on the left; collapsible stream panels on the right; selected-event detail at the bottom. Matches current TUI layout but in a browser.

**Requires:** npm, Vite, TypeScript reducer port, bundled assets checked in or built at release.

**Value:** Full parity with the Rich TUI. At this point the Rich TUI can be marked deprecated.

### Phase C — History, search, expansion

**Deliverable:** History view (`/`); full-text search across a run's transcript; time-travel scrubber (jump to any event offset); side-by-side run comparison.

**Value:** Moves well beyond current TUI capability. Enables post-mortem debugging across runs.

Each phase ships as a tagged release increment (`godel[web]` optional group). Phases A and B each provide standalone value and can be validated with real usage before committing to the next phase.

---

## 7. Risks

### 7.1 Frontend stack maintenance burden

A React + TypeScript + Vite frontend is a meaningfully heavier maintenance surface than `rich`. npm dependency churn, security audits, bundler upgrades, and React major versions all require ongoing attention. Mitigation: pin exact versions, use `npm ci`, run `npm audit` in CI.

### 7.2 Distribution complexity

Shipping bundled frontend assets inside a Python wheel is non-standard. The wheel will include `godel/web/dist/` (~500 KB), which must be rebuilt and committed before each release. A missing build step will ship a stale UI. Mitigation: CI enforces that `dist/` is up to date (hash check); release workflow runs `npm run build` before packaging.

### 7.3 TypeScript reducer drift

If the Python `reduce()` function evolves (new ops, new fields) and the TS port is not updated, the browser UI silently ignores new events. Mitigation: shared transcript fixture test suite run against both the Python and TS reducers in CI. Any new op in `_watch_model.py` must include a corresponding TS change.

### 7.4 Dependency weight for a CLI tool

Adding `fastapi` + `uvicorn` as optional deps is reasonable (they are standard, well-maintained). The core `godel` package remains import-clean (no new required deps). `pip install 'godel[web]'` is the opt-in.

### 7.5 Port conflicts

`--port 0` (OS-assigned) avoids conflicts but produces a non-deterministic URL. Mitigation: the CLI prints the URL at startup (`Listening on http://localhost:54321`) and opens it in the browser automatically.

---

## 8. Decision Matrix

| Option | Cost | Benefit | Risk | Reversibility |
|--------|------|---------|------|---------------|
| **A. Stay on Rich TUI** | None (status quo) | None — existing friction persists. Panel bloat, no expand/collapse, no history. | Low — known codebase; no new deps. | N/A — can pivot later, but tech debt grows as workflows get more complex. |
| **B. Hybrid — thin TUI + web GUI** | Medium. Phase A: ~2 days. Phase B: ~1 week. Phase C: ~2 weeks. Frontend build pipeline (~0.5 days setup). Total: ~3–4 weeks for full Phase B. | High. Retains plain terminal for SSH/CI. Adds browser-native expand/collapse, full payload, history, shareable URL. Rich TUI deprecated after Phase B. | Medium. Two rendering surfaces to maintain during transition. TS reducer drift if not gated in CI. | High — plain log path always survives. Web UI can be removed if unmaintained. |
| **C. Full pivot — delete TUI** | High initially. Must reach Phase B before `godel watch` is usable for TTY users. SSH/CI path is `_PlainLineLog` (survives). Rich removed. ~3–4 weeks. | High. ~500 LOC deleted from `_watch.py`. Single maintained rendering surface. | Higher. Breaks `godel[watch]` users who have not installed `godel[web]`. Window of regression between TUI removal and web Phase B shipping. | Low — once Rich is deleted and users move to `[web]`, rollback requires reinstating the Rich code. |

**Notes:**
- "Cost" is implementation time, not ongoing maintenance.
- Ongoing maintenance for Option B/C (web) is heavier than A (Rich only), offset by dramatically better observability.
- Option C's reversal cost is high once the `rich` code is deleted and users have migrated.

---

## 9. Recommendation

**Adopt Option B — Hybrid: thin TUI + web GUI.**

### Reasoning

The fundamental problem is structural: a terminal panel layout cannot provide expand/collapse, full payload inspection, search, or history. These are browser-native capabilities. Building them in Rich would require a TUI framework upgrade (e.g. Textual) that is comparably complex to a browser frontend — with worse rendering, no shareable URL, and no ecosystem of JSON/diff viewer components.

The hybrid path avoids a risky all-or-nothing transition. Phase A (server + raw SSE log, ~2 days) ships value immediately with zero frontend build complexity: any browser can observe a live run with full event payloads visible. Phase B (structured React UI, ~1 week) reaches TUI parity in the browser. At that point, `godel[watch]` is marked deprecated but not deleted — users on SSH-only or CI environments still get `_PlainLineLog` without any web server.

Full deletion (Option C) is appealing for simplicity but the risk window is real: there will be a period where `godel watch` requires `godel[web]` or falls back to plain log, and that is a regression for users who relied on the Rich TUI. Delaying deletion until Phase B ships and has been validated in production is more prudent.

**Concrete next steps if this proposal is accepted:**

1. Create implementation ticket for Phase A (server + vanilla SSE HTML, ~2 days).
2. Create implementation ticket for Phase B (React SPA + TS reducer, ~1 week).
3. Mark `godel[watch]` as deprecated in `pyproject.toml` and in the CLI help text after Phase B ships.
4. Create deletion ticket for Phase C (remove Rich code, remove `godel[watch]` group) with a milestone no earlier than 60 days after Phase B.

If this proposal is rejected in favour of staying on the Rich TUI (Option A), close this ticket and file a targeted ticket to address the panel-bloat problem within the existing Rich framework (e.g. integrate Textual for collapsible panels, or add a `godel watch --expand-all` flag that dumps full payloads to a scroll buffer).

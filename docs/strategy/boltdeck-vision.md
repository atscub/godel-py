# Boltdeck — mission control for agentic work

*Snapshot: 2026-04. Raw product brainstorm, not validated. The only doc capturing the GUI / command-centre direction.*

Boltdeck is a separate project — an Electron desktop GUI at `../boltdeck`, currently a passive viewer/manager for Claude Code sessions. The product opportunity is evolving it into an **active command centre** for agentic work, with Godel as the orchestration engine underneath.

## The pain points it addresses

From user experience with agentic tooling today:

- Too many terminals and IDEs open — context sprawl across projects and agents.
- Poor visibility across agents — what's happening, where, right now?
- Silent completions — agent finishes at 3am, you wake up to 12 changed files with no summary.
- No prioritisation — five agents running, which one needs attention *now*?
- Lost context between sessions — agent finishes, conversation is 500 turns deep.
- No coordination — two agents edit the same file, merge conflicts.
- Unclear cost accumulation — burning tokens across sessions with no aggregate view.
- No way to delegate and trust — you babysit because there's no notification/approval layer.
- No workflow memory — "do what you did last time for PR review" doesn't exist.

## Three layers

**Layer 1 — Dashboard (see everything).** A bird's-eye view of what Boltdeck already does per-session.

- All active agents across all projects, one screen.
- Per-agent status: idle / working / needs attention / done / failed.
- Notifications — toast, sound, badge on completion or block.
- Aggregate cost across sessions.
- Timeline: what happened while you were away.
- Priority sorting: what needs human attention first.

**Layer 2 — Orchestration (direct the work). This is where Godel becomes the engine.**

- Launch agents from the UI, not just resume sessions.
- Queue work ("after A finishes, start B on this").
- Templates: "PR review workflow" = spawn agent with these instructions.
- Approval gates: agent pauses, asks for human sign-off before proceeding.
- Workflows defined as `.py` files (or `.gdl` once that lands), executed from the UI through the Godel runtime.
- The UI is the control surface; Godel is the execution engine. Boltdeck's orchestration capabilities grow as the library grows — no UI code changes needed.

**Layer 3 — Intelligence (learn and improve).**

- Run summaries: what changed, what was decided, what failed.
- Cross-session memory: "last time you reviewed this repo, you caught X".
- Cost analytics: which workflows are expensive, which are efficient.
- Quality signals: did the user accept/reject agent output?
- Pattern detection: "this type of task always takes 3 retries".

## The adapter pattern

Boltdeck currently hard-codes Claude Code. Decouple:

```
+-----------------------------+
|      Boltdeck UI            |
+-----------------------------+
|      Agent Adapter API      |
+------+------+-------+-------+
|Claude|Cursor|Aider  |Custom |
|Code  |Agent |       |(Godel)|
+------+------+-------+-------+
```

Each adapter implements: `listSessions()`, `spawnAgent(config)`, `getConversation(sessionId)`, `getFileChanges(sessionId)`, `sendMessage(sessionId, msg)`, `getStatus(sessionId)`, `getCost(sessionId)`. Claude Code adapter is what Boltdeck does today; others follow the same interface. Godel slots in as an adapter — a "custom" agent whose sessions are Godel runs.

## Why this can be *the* product

| Test | Answer |
|---|---|
| Do people already pay for this? | Yes — Cursor ($20/mo), Windsurf, GitHub Copilot Workspace. The "AI coding UI" market is validated. |
| Is Godel the leverage? | Yes — Layer 2 is where Godel shines. Workflows as files, not hard-coded UI logic. |
| Can one person build it? | Yes — Boltdeck already exists. Electron + React stack is familiar. |
| Is it defensible? | Adapter pattern + Godel orchestration is hard to replicate. Individual features are copyable; the integrated experience isn't. |
| Revenue model? | Free: 1–2 agents, local only. Paid: unlimited agents, cloud sync, team dashboards, workflow templates. |

Positioning vs IDE harnesses: Cursor, Cognition, Factory, Augment, Replit own the *editor*. Boltdeck owns the *command centre*. Different job; not a competitor.

## Open questions

- MVP scope — ship Layer 1 first (closest to current Boltdeck), or go straight to Layer 2 with Godel?
- Desktop vs web? Electron gives PTY and filesystem access; web limits distribution in the other direction.
- Adapter priority — Claude Code only, add others as demand appears?
- Pricing tiers — free tier generous enough to hook, paid justified by orchestration / templates / team features?
- When does Layer 2 (Godel integration) become the priority — after Layer 1 has users?

*Source: brainstorm-boltdeck-product.md.*

# Agent Instructions
This repository is the **Python implementation** of Godel — a deterministic orchestrator for AI agent workflows. Users write workflows as plain Python, decorated with `@workflow` and `@step`; the runtime handles event logging, pause/resume, rewind, and deterministic replay.

This repo contains the Python library and its CLI.

## Project structure

```
godel-py/
├── godel/              # Package source (CLI, decorators, event log, agents, intervention)
├── tests/              # pytest suite
├── docs/               # User guides, API reference, concepts, examples
├── examples/           # End-to-end example workflows
├── .agents/CLI.md      # godel CLI command reference
├── .agents/HANDOFF.md  # Technical handoff / milestone context
├── .agents/MONITORING.md # How to monitor a live workflow run efficiently
├── pyproject.toml      # Package metadata + semantic-release config
└── .github/workflows/  # CI: tests + release on merge to main
```

## Key modules

- `godel/cli.py` — `godel` command entry point
- `godel/_decorators.py` — `@workflow`, `@step`, `parallel`, `retry`
- `godel/_event_log.py` — append-only event log (deterministic replay backbone)
- `godel/_run.py` — `run()` for shelling out to CLI tools (agents, git, etc.)
- `godel/agents/` — Claude, Copilot agent wrappers
- `godel/intervention/` — repair / human-in-the-loop tooling

## How to work on this project

- Python 3.10+; `pip install -e ".[dev]"` for dev setup.
- `pytest` for tests; keep the suite green before pushing.
- Conventional commits are required — version bumps and releases are automated.
- Use `bd` (beads) for task tracking. See `AGENTS.md`.

## Updating docs

`docs/` is the authoritative copy. `godel/_guides/` is a bundled duplicate used by `godel guide` inside installed wheels. **After editing any file in `docs/`, run:**

```bash
bash scripts/sync_guides.sh
```

Then commit both `docs/` and `godel/_guides/` together.

## Release

Pushes to `main` run `.github/workflows/publish.yml`:
1. Install deps + run `pytest`
2. `python-semantic-release` inspects commits, bumps version, creates a tag + GitHub Release, and uploads `.whl` + `.tar.gz` assets.

Releases are published as GitHub Releases.


## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Using beads
When working in medium-large tasks, use beads to decomponse or track the progress. When you need, read [BEADS.md](.agents/BEADS.md)

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

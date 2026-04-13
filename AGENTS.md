# Agent Instructions
This repository is the **Python implementation** of Godel — a deterministic orchestrator for AI agent workflows. Users write workflows as plain Python, decorated with `@workflow` and `@step`; the runtime handles event logging, pause/resume, rewind, and deterministic replay.

The language spec, grammar, and design docs live in [atscub/godel-lang](https://github.com/atscub/godel-lang). This repo only contains the Python library and its CLI.

## Project structure

```
godel-py/
├── godel/              # Package source (CLI, decorators, event log, agents, intervention)
├── tests/              # pytest suite
├── docs/               # User guides, API reference, concepts, examples
├── examples/           # End-to-end example workflows
├── .agents/CLI.md      # godel CLI command reference
├── .agents/HANDOFF.md  # Technical handoff / milestone context
├── pyproject.toml      # Package metadata + semantic-release config
└── .github/workflows/  # CI: tests + release on merge to master
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

## Release

Pushes to `master` run `.github/workflows/publish.yml`:
1. Install deps + run `pytest`
2. `python-semantic-release` inspects commits, bumps version, creates a tag + GitHub Release, and uploads `.whl` + `.tar.gz` assets.

Private repo → releases are authenticated.


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

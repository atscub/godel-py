# Contributing

## Setup

```bash
pip install -e ".[dev]"
pytest
```

## Workflow

1. Find work: `bd ready` (see `AGENTS.md` for beads rules).
2. Branch off `master`.
3. Commit using **conventional commits** — this drives releases:
   - `feat: ...` → minor bump
   - `fix: ...` / `perf: ...` → patch bump
   - `feat!: ...` or `BREAKING CHANGE:` footer → major bump
   - `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `style:` → no release
4. Open a PR to `master`. Tests must pass.
5. On merge, `.github/workflows/publish.yml` tags, builds, and publishes a new GitHub Release automatically.

## Style

- `ruff` for lint.
- Keep public API surfaces in `godel/__init__.py` stable; breaking changes require `feat!:`.

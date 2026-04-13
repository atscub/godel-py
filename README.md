# godel-py

Python implementation of **Godel** — a deterministic orchestrator for AI agent workflows. Workflows are plain Python decorated with `@workflow` / `@step`, with automatic event logging, pause/resume, and rewind.

The language spec and design docs live in the companion repo: [atscub/godel-lang](https://github.com/atscub/godel-lang).

## Install

This package is published as a private GitHub Release asset. You need a Personal Access Token with `repo` scope on `atscub/godel-py`.

```bash
export GH_TOKEN=ghp_xxx
pip install "https://${GH_TOKEN}@github.com/atscub/godel-py/releases/download/v0.1.0/godel-0.1.0-py3-none-any.whl"
```

Or install the latest `master` directly:

```bash
pip install "git+https://${GH_TOKEN}@github.com/atscub/godel-py.git@master"
```

## Quick start

```python
from godel import workflow, step, run

@step
def build():
    return run(["npm", "run", "build"])

@workflow
def ci():
    build()
```

```bash
godel run ci.py
```

See [docs/why-godel.md](docs/why-godel.md) for the thesis, `.agents/CLI.md` for the full CLI reference, and `docs/` for user guides, API reference, and examples.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Requires Python 3.10+.

## Release process

Merges to `master` trigger `.github/workflows/publish.yml`, which runs tests and then `python-semantic-release`. Conventional commits (`feat:`, `fix:`, `feat!:`) drive version bumps, tag creation, changelog generation, and wheel+sdist upload to a private GitHub Release.

## License

Proprietary — internal use only.

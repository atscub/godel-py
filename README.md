# godel-py

Python implementation of **Godel** — a deterministic orchestrator for AI agent workflows. Workflows are plain Python decorated with `@workflow` / `@step`, with automatic event logging, pause/resume, and rewind.

The language spec and design docs live in the companion repo: [atscub/godel-lang](https://github.com/atscub/godel-lang).

## Install

```bash
pip install git+https://github.com/atscub/godel-py.git@master
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

Merges to `master` trigger `.github/workflows/publish.yml`, which runs tests and then `python-semantic-release`. Conventional commits (`feat:`, `fix:`, `feat!:`) drive version bumps, tag creation, changelog generation, and wheel+sdist upload to a GitHub Release.

## License

Business Source License 1.1 — see [LICENSE](LICENSE) for details. You are free to use Godel as a library in your own applications, including commercially. The only restriction is offering Godel itself as a competing hosted orchestration service. After six years, each version converts to Apache 2.0.

# Godel Documentation Index

Godel is a deterministic orchestrator for AI-agent workflows. See
[`README.md`](README.md) for the full narrative introduction and contents list.

## Guides

- [Why Godel](why-godel.md)
- [Getting Started](getting-started.md)
- [Concepts](concepts.md)
- [API Reference](api-reference.md)
- [CLI Reference](cli.md)
- [Examples](examples.md)

## Observability

Godel writes a live observability stream (the **transcript**) alongside the authoritative
audit log. Three focused guides cover the wire format, redaction, and stdout capture:

- [Transcript Format](transcript-format.md) — JSONL v1 wire contract: header, event
  field table, `stream_path` semantics, rotation sentinel (no `seq`), `.N` chain, semver
  policy, and an annotated example.
- [Redaction](redaction.md) — registering redactors via `@workflow(redact=[...])`,
  intended composition order, `redactor.error` event semantics, and the "Godel does not
  guess at secrets" disclaimer. **Status: decoration-time validation only; runtime
  pipeline tracked by `godel-py-5pl.6`.**
- [Stdout Capture](stdout-capture.md) — `@step(capture_stdout=True)` and
  `@workflow(capture_stdout=True)`, the pipe-per-step model, `parallel()`
  incompatibility (enforced at `parallel()` call time today), the `GODEL_NO_CAPTURE=1`
  escape hatch, and interactive-debugger caveats. **Status: kwarg accepted;
  pipe/reader-thread runtime tracked by `godel-py-5pl.7`.**

## Other resources

- [Agent Skills](skills/README.md)
- [Strategy notes](strategy/README.md)

#!/usr/bin/env bash
# Sync bundled agent guides (godel/_guides/) from the canonical docs/ sources.
# docs/ is the authoritative, human-facing copy; godel/_guides/ is what ships
# inside the installed wheel so `godel guide` works without the repo present.
set -euo pipefail

cd "$(dirname "$0")/.."

cp docs/skills/godel-engineer.md godel/_guides/engineer.md
cp docs/skills/godel-runner.md   godel/_guides/runner.md
cp docs/cli.md                   godel/_guides/cli.md
cp docs/concepts.md              godel/_guides/concepts.md
cp docs/api-reference.md         godel/_guides/api-reference.md
cp docs/getting-started.md       godel/_guides/getting-started.md
cp docs/monitoring.md            godel/_guides/monitoring.md

echo "synced 7 guides from docs/ -> godel/_guides/"

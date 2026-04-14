"""Tests for the ``godel guide`` bundled-docs command."""
from click.testing import CliRunner

from godel.cli import main
from godel._guides import GUIDES, GODEL_BLURB


def test_guide_no_arg_prints_blurb_and_index():
    result = CliRunner().invoke(main, ["guide"])
    assert result.exit_code == 0
    assert "deterministic orchestrator" in result.output
    for slug, _ in GUIDES:
        assert slug in result.output


def test_guide_unknown_name_errors():
    result = CliRunner().invoke(main, ["guide", "does-not-exist"])
    assert result.exit_code == 1
    assert "unknown guide" in result.output


def test_guide_each_bundled_guide_renders():
    r = CliRunner()
    for slug, _ in GUIDES:
        result = r.invoke(main, ["guide", slug])
        assert result.exit_code == 0, f"{slug}: {result.output}"
        # Each guide's markdown is non-empty and starts with a heading.
        assert len(result.output.strip()) > 100, f"{slug} too short"


def test_blurb_mentions_core_concepts():
    for kw in ("@workflow", "@step", "audit log"):
        assert kw in GODEL_BLURB


def test_bundled_guides_match_docs_source():
    """``godel/_guides/`` must stay byte-identical to ``docs/`` sources.

    If this fails, run ``scripts/sync_guides.sh`` (or re-copy the file).
    """
    from pathlib import Path
    repo = Path(__file__).parent.parent
    mapping = {
        "engineer.md":        repo / "docs" / "skills" / "godel-engineer.md",
        "runner.md":          repo / "docs" / "skills" / "godel-runner.md",
        "cli.md":             repo / "docs" / "cli.md",
        "concepts.md":        repo / "docs" / "concepts.md",
        "api-reference.md":   repo / "docs" / "api-reference.md",
        "getting-started.md": repo / "docs" / "getting-started.md",
    }
    guides_dir = repo / "godel" / "_guides"
    for bundled, source in mapping.items():
        assert (guides_dir / bundled).read_bytes() == source.read_bytes(), (
            f"{bundled} out of sync with {source} — re-run scripts/sync_guides.sh"
        )

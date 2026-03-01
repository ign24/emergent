from __future__ import annotations

from datetime import UTC, datetime

from emergent.research.formatter import generate_markdown_report, generate_telegram_digest
from emergent.research.types import ResearchFinding, ScoredFinding


def _scored(title: str, score: float) -> ScoredFinding:
    return ScoredFinding(
        finding=ResearchFinding(
            source="github",
            domain="emergent_deps",
            title=title,
            url=f"https://example.com/{title}",
            summary="release notes",
            published_at=datetime.now(UTC),
            metadata={"stars": 100},
        ),
        score=score,
    )


def test_generate_markdown_report_contains_sections() -> None:
    report = generate_markdown_report(
        run_id="abc123",
        highlights=[_scored("Top finding", 0.91)],
        rest=[_scored("Other finding", 0.55)],
        total_sources=6,
    )
    assert "## Highlights" in report
    assert "## All Findings by Domain" in report
    assert "Run ID: abc123" in report


def test_generate_telegram_digest_truncates() -> None:
    digest = generate_telegram_digest(
        highlights=[_scored("A" * 200, 0.8)],
        rest_count=20,
        report_relpath="data/research/2026-02-22.md",
        max_chars=120,
    )
    assert len(digest) <= 120


def test_generate_markdown_report_supports_report_style_sections() -> None:
    report = generate_markdown_report(
        run_id="abc123",
        highlights=[_scored("Top finding", 0.91)],
        rest=[],
        total_sources=6,
        report_style="academic-researcher",
    )
    assert "## Methodology" in report
    assert "## Evidence Quality Notes" in report

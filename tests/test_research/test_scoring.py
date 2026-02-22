from __future__ import annotations

from datetime import UTC, datetime, timedelta

from emergent.research.scoring import score_finding, separate_highlights
from emergent.research.types import ResearchFinding, ScoredFinding


def _finding(*, source: str, published_days_ago: int, title: str, summary: str) -> ResearchFinding:
    return ResearchFinding(
        source=source,
        domain="agent_architecture",
        title=title,
        url=f"https://example.com/{source}/{published_days_ago}/{title.replace(' ', '-')}",
        summary=summary,
        published_at=datetime.now(UTC) - timedelta(days=published_days_ago),
        metadata={"stars": 500, "points": 100, "score": 200},
    )


def test_score_finding_prefers_recent_and_authoritative() -> None:
    recent = _finding(
        source="arxiv",
        published_days_ago=1,
        title="Agent orchestration safety",
        summary="agent tool orchestration",
    )
    old = _finding(
        source="reddit",
        published_days_ago=20,
        title="Agent discussion",
        summary="agent",
    )

    recent_score = score_finding(recent, ["agent", "orchestration", "tool"])
    old_score = score_finding(old, ["agent", "orchestration", "tool"])
    assert recent_score > old_score


def test_separate_highlights_threshold_and_limit() -> None:
    scored = [
        ScoredFinding(_finding(source="arxiv", published_days_ago=1, title="A", summary="A"), 0.9),
        ScoredFinding(_finding(source="github", published_days_ago=1, title="B", summary="B"), 0.8),
        ScoredFinding(_finding(source="hn", published_days_ago=1, title="C", summary="C"), 0.6),
        ScoredFinding(_finding(source="reddit", published_days_ago=1, title="D", summary="D"), 0.2),
    ]

    highlights, rest = separate_highlights(scored, threshold=0.7, max_highlights=1)
    assert len(highlights) == 1
    assert highlights[0].score == 0.9
    assert len(rest) == 2

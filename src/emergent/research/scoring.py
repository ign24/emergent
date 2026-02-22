"""Deterministic scoring and highlight selection for research findings."""

from __future__ import annotations

from datetime import UTC, datetime

from emergent.research.types import ResearchFinding, ScoredFinding

SOURCE_AUTHORITY: dict[str, float] = {
    "arxiv": 1.0,
    "github": 0.9,
    "tavily": 0.7,
    "hn": 0.7,
    "rss": 0.6,
    "reddit": 0.5,
}


def _normalize_engagement(source: str, metadata: dict[str, object]) -> float:
    if source == "github":
        stars = int(metadata.get("stars", 0))
        return min(1.0, stars / 1000)
    if source == "hn":
        points = int(metadata.get("points", 0))
        return min(1.0, points / 500)
    if source == "reddit":
        score = int(metadata.get("score", 0))
        return min(1.0, score / 1000)
    if source == "arxiv":
        return 0.5
    return 0.3


def _keyword_match_score(text: str, domain_keywords: list[str]) -> float:
    if not domain_keywords:
        return 0.4
    tokens = {t.strip().lower() for t in text.split() if t.strip()}
    if not tokens:
        return 0.0
    matches = sum(1 for keyword in domain_keywords if keyword.lower() in tokens)
    return min(1.0, matches / max(1, len(domain_keywords)))


def score_finding(finding: ResearchFinding, domain_keywords: list[str]) -> float:
    """Score finding from 0.0 to 1.0 using deterministic heuristics."""
    now = datetime.now(UTC)
    score = 0.0

    age_days = 15
    if finding.published_at is not None:
        published = finding.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_days = max(0, (now - published).days)
    recency = max(0.0, 1.0 - (age_days / 30))
    score += 0.40 * recency

    engagement = _normalize_engagement(finding.source, finding.metadata)
    score += 0.25 * engagement

    relevance = _keyword_match_score(
        text=f"{finding.title} {finding.summary}",
        domain_keywords=domain_keywords,
    )
    score += 0.25 * relevance

    authority = SOURCE_AUTHORITY.get(finding.source, 0.5)
    score += 0.10 * authority

    return round(max(0.0, min(1.0, score)), 4)


def separate_highlights(
    findings: list[ScoredFinding],
    *,
    threshold: float,
    max_highlights: int,
) -> tuple[list[ScoredFinding], list[ScoredFinding]]:
    """Split scored findings into highlights and standard findings."""
    sorted_findings = sorted(findings, key=lambda item: item.score, reverse=True)
    highlights = [f for f in sorted_findings if f.score >= threshold][:max_highlights]
    rest = [f for f in sorted_findings if f not in highlights and f.score >= 0.3]
    return highlights, rest

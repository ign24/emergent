"""Data models for the research worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SourceItem:
    """Normalized source document collected during discovery."""

    source: str
    external_id: str
    title: str
    url: str
    published_at: datetime | None
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """Actionable finding extracted from one or more source items."""

    finding_id: str
    title: str
    summary: str
    impact: float
    relevance: float
    novelty: float
    confidence: float
    evidence_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResearchReport:
    """Final report output produced by the worker."""

    run_id: str
    generated_at: datetime
    top_findings: list[Finding]
    additional_findings: list[Finding]
    watchlist: list[Finding]
    markdown: str

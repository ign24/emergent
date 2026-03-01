"""Shared dataclasses for research worker pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ResearchFinding:
    """Normalized finding collected from any research source."""

    source: str
    domain: str
    title: str
    url: str
    summary: str
    published_at: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredFinding:
    """Research finding enriched with deterministic relevance score."""

    finding: ResearchFinding
    score: float

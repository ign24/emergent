"""Tavily source adapter placeholder."""

from __future__ import annotations

from emergent.workers.research.models import SourceItem


class TavilySourceAdapter:
    """Collect source documents from Tavily search API."""

    async def collect(self, query: str, limit: int) -> list[SourceItem]:
        """Collect source items from Tavily."""
        _ = (query, limit)
        return []

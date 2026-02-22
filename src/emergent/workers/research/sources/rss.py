"""RSS/Atom source adapter placeholder."""

from __future__ import annotations

from emergent.workers.research.models import SourceItem


class RssSourceAdapter:
    """Collect source documents from RSS or Atom feeds."""

    async def collect(self, query: str, limit: int) -> list[SourceItem]:
        """Collect source items from configured feeds."""
        _ = (query, limit)
        return []

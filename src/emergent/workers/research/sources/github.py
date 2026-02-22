"""GitHub source adapter placeholder."""

from __future__ import annotations

from emergent.workers.research.models import SourceItem


class GitHubSourceAdapter:
    """Collect source documents from GitHub APIs."""

    async def collect(self, query: str, limit: int) -> list[SourceItem]:
        """Collect source items from GitHub."""
        _ = (query, limit)
        return []

"""Base protocol for research source adapters."""

from __future__ import annotations

from typing import Protocol

from emergent.workers.research.models import SourceItem


class SourceAdapter(Protocol):
    """Adapter interface for collecting source items asynchronously."""

    async def collect(self, query: str, limit: int) -> list[SourceItem]:
        """Collect normalized source items for a query."""

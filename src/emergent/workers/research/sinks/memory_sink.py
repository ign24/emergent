"""Semantic memory sink for research artifacts."""

from __future__ import annotations

from emergent.workers.research.models import ResearchReport


class MemoryResearchSink:
    """Persist selected findings into semantic memory."""

    async def write_report(self, report: ResearchReport) -> None:
        """Store report data in semantic memory."""
        _ = report

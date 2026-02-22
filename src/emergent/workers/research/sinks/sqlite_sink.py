"""SQLite persistence sink for research artifacts."""

from __future__ import annotations

from emergent.workers.research.models import ResearchReport


class SqliteResearchSink:
    """Persist reports and findings to SQLite tables."""

    async def write_report(self, report: ResearchReport) -> None:
        """Persist report artifacts."""
        _ = report

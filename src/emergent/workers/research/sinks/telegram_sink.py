"""Telegram digest sink for research reports."""

from __future__ import annotations

from emergent.workers.research.models import ResearchReport


class TelegramResearchSink:
    """Send concise report digests to Telegram."""

    async def write_report(self, report: ResearchReport) -> None:
        """Send digest message(s)."""
        _ = report

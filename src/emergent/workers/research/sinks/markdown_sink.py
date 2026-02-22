"""Markdown filesystem sink for research reports."""

from __future__ import annotations

import asyncio
from pathlib import Path

from emergent.workers.research.models import ResearchReport


class MarkdownResearchSink:
    """Write report markdown to the filesystem."""

    def __init__(self, reports_dir: Path) -> None:
        self._reports_dir = reports_dir

    async def write_report(self, report: ResearchReport) -> None:
        """Write markdown content to a timestamped file."""
        await asyncio.to_thread(self._reports_dir.mkdir, parents=True, exist_ok=True)
        path = self._reports_dir / f"{report.run_id}.md"
        await asyncio.to_thread(path.write_text, report.markdown, encoding="utf-8")

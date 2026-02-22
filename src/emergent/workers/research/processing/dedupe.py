"""Deduplication helpers for normalized source items."""

from __future__ import annotations

from emergent.workers.research.models import SourceItem


def dedupe_by_url(items: list[SourceItem]) -> list[SourceItem]:
    """Deduplicate source items by canonical URL."""
    seen: set[str] = set()
    deduped: list[SourceItem] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(item)
    return deduped

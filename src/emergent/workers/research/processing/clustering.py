"""Clustering helpers for grouping related findings."""

from __future__ import annotations

from collections import defaultdict

from emergent.workers.research.models import Finding


def cluster_by_primary_tag(findings: list[Finding]) -> dict[str, list[Finding]]:
    """Group findings by first tag, defaulting to 'uncategorized'."""
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        key = finding.tags[0] if finding.tags else "uncategorized"
        grouped[key].append(finding)
    return dict(grouped)

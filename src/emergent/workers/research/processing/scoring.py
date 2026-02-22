"""Scoring helpers for research findings."""

from __future__ import annotations


def compute_final_score(
    *, impact: float, relevance: float, novelty: float, confidence: float
) -> float:
    """Compute weighted score in [0, 1] for ranking findings."""
    value = 0.35 * impact + 0.25 * relevance + 0.20 * novelty + 0.20 * confidence
    return max(0.0, min(1.0, value))

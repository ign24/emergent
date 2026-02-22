"""Deterministic routing and gating policy for research runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchPolicy:
    """Thresholds and limits used to decide report depth and model tier."""

    novelty_similarity_threshold: float = 0.82
    high_impact_threshold: float = 0.70
    min_high_impact_findings_for_deep_report: int = 2


@dataclass(frozen=True)
class GateDecision:
    """Decision result for deep report generation."""

    should_run_deep_report: bool
    reason: str


def evaluate_deep_report_gate(
    *,
    policy: ResearchPolicy,
    max_similarity_to_recent_runs: float,
    high_impact_findings: int,
    has_critical_signal: bool,
) -> GateDecision:
    """Decide whether the weekly run should escalate to deep synthesis."""
    if (
        max_similarity_to_recent_runs >= policy.novelty_similarity_threshold
        and not has_critical_signal
    ):
        return GateDecision(
            should_run_deep_report=False,
            reason="low_novelty",
        )

    if high_impact_findings < policy.min_high_impact_findings_for_deep_report:
        return GateDecision(
            should_run_deep_report=False,
            reason="insufficient_high_impact",
        )

    return GateDecision(
        should_run_deep_report=True,
        reason="gates_passed",
    )

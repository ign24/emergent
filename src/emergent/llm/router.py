"""Deterministic model routing for cost-aware orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from emergent.config import ModelTier


@dataclass(frozen=True)
class RoutingDecision:
    tier: ModelTier
    reason: str


class ModelRouter:
    """Select a model tier using deterministic heuristics.

    The router is intentionally simple and non-LLM based to keep routing cheap,
    debuggable, and predictable.
    """

    _STRONG_KEYWORDS = {
        "refactor",
        "refactorizar",
        "analyze",
        "analizar",
        "diagnose",
        "diagnosticar",
        "debug",
        "investigar",
        "comparar",
        "compare",
        "optimize",
        "optimizar",
        "design",
        "disenar",
        "tests",
        "test suite",
        "worker",
        "workers",
        "orchestrator",
        "orquestador",
        "delegar",
        "delegate",
        "multi-step",
        "multistep",
    }
    _FAST_KEYWORDS = {
        "ls",
        "pwd",
        "date",
        "whoami",
        "status",
        "estado",
        "list",
        "listar",
        "mostra",
        "mostrar",
        "abrir",
        "open",
    }

    def decide(
        self,
        *,
        user_message: str,
        turn_count: int,
        has_tier2_tools: bool,
        routing_enabled: bool,
    ) -> RoutingDecision:
        if not routing_enabled:
            return RoutingDecision(tier=ModelTier.DEFAULT, reason="routing_disabled")

        msg = user_message.strip().lower()
        msg_len = len(msg)

        if msg_len > 500:
            return RoutingDecision(tier=ModelTier.STRONG, reason="long_prompt")

        if any(keyword in msg for keyword in self._STRONG_KEYWORDS):
            return RoutingDecision(tier=ModelTier.STRONG, reason="strong_keyword")

        if has_tier2_tools:
            return RoutingDecision(tier=ModelTier.DEFAULT, reason="safety_sensitive_tools")

        if turn_count >= 5:
            return RoutingDecision(tier=ModelTier.DEFAULT, reason="long_conversation")

        if msg_len <= 80 and any(keyword in msg for keyword in self._FAST_KEYWORDS):
            return RoutingDecision(tier=ModelTier.FAST, reason="fast_keyword")

        return RoutingDecision(tier=ModelTier.DEFAULT, reason="default")

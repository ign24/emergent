from __future__ import annotations

from emergent.config import ModelTier
from emergent.llm.router import ModelRouter


def test_router_uses_fast_for_short_command() -> None:
    router = ModelRouter()

    decision = router.decide(
        user_message="ls",
        turn_count=0,
        has_tier2_tools=False,
        routing_enabled=True,
    )

    assert decision.tier == ModelTier.FAST
    assert decision.reason == "fast_keyword"


def test_router_uses_strong_for_complex_prompt() -> None:
    router = ModelRouter()

    decision = router.decide(
        user_message="Analizar y refactorizar este modulo con tests",
        turn_count=1,
        has_tier2_tools=False,
        routing_enabled=True,
    )

    assert decision.tier == ModelTier.STRONG
    assert decision.reason == "strong_keyword"


def test_router_returns_default_when_disabled() -> None:
    router = ModelRouter()

    decision = router.decide(
        user_message="refactor this",
        turn_count=3,
        has_tier2_tools=True,
        routing_enabled=False,
    )

    assert decision.tier == ModelTier.DEFAULT
    assert decision.reason == "routing_disabled"

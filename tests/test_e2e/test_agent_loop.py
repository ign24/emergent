"""Live E2E tests for AgentRuntime against Anthropic API."""

from __future__ import annotations

import os

import pytest

from emergent.agent.runtime import AgentRuntime
from emergent.config import (
    AgentConfig,
    EmergentSettings,
    ModelTier,
    ModelTierConfig,
    ProviderConfig,
)

pytestmark = pytest.mark.e2e


def _require_api_key() -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key.startswith("sk-"):
        pytest.skip("ANTHROPIC_API_KEY is required for live e2e tests")
    return api_key


def _provider() -> str:
    return os.getenv("EMERGENT_E2E_PROVIDER", "anthropic").strip().lower()


@pytest.mark.asyncio
async def test_live_round_trip_text_response() -> None:
    provider = _provider()
    api_key = _require_api_key() if provider == "anthropic" else ""
    selected_model = os.getenv("EMERGENT_E2E_MODEL", "claude-haiku-4-5-20251001")
    agent = AgentConfig(
        providers={
            "anthropic": ProviderConfig(api_key_env="ANTHROPIC_API_KEY"),
            "ollama": ProviderConfig(
                base_url=os.getenv("EMERGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            ),
        },
        models={
            ModelTier.DEFAULT.value: ModelTierConfig(
                provider=provider,
                model=selected_model,
                max_tokens=128,
            ),
            ModelTier.FAST.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
            ModelTier.STRONG.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
            ModelTier.SUMMARY.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
        },
        routing_enabled=False,
    )
    settings = EmergentSettings(
        anthropic_api_key=api_key,
        agent=agent,
    )
    runtime = AgentRuntime(settings=settings)
    try:
        text, trace = await runtime.run(
            user_message="Respond with EXACT text: E2E_OK",
            session_id="e2e-live-session",
        )
    finally:
        await runtime.close()

    assert text.strip() == "E2E_OK"
    assert trace["success"] is True
    assert trace["iterations"] >= 1


@pytest.mark.asyncio
@pytest.mark.expensive
async def test_live_latency_budget_under_60s() -> None:
    provider = _provider()
    api_key = _require_api_key() if provider == "anthropic" else ""
    selected_model = os.getenv("EMERGENT_E2E_MODEL", "claude-haiku-4-5-20251001")
    agent = AgentConfig(
        providers={
            "anthropic": ProviderConfig(api_key_env="ANTHROPIC_API_KEY"),
            "ollama": ProviderConfig(
                base_url=os.getenv("EMERGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            ),
        },
        models={
            ModelTier.DEFAULT.value: ModelTierConfig(
                provider=provider,
                model=selected_model,
                max_tokens=128,
            ),
            ModelTier.FAST.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
            ModelTier.STRONG.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
            ModelTier.SUMMARY.value: ModelTierConfig(
                provider=provider, model=selected_model, max_tokens=128
            ),
        },
        routing_enabled=False,
    )
    settings = EmergentSettings(
        anthropic_api_key=api_key,
        agent=agent,
    )
    runtime = AgentRuntime(settings=settings)
    try:
        text, trace = await runtime.run(
            user_message="Respond with EXACT text: LATENCY_OK",
            session_id="e2e-latency-session",
        )
    finally:
        await runtime.close()

    assert text.strip() == "LATENCY_OK"
    assert trace["success"] is True
    assert trace["duration_ms"] < 60_000

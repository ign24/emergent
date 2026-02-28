"""Live E2E tests for AgentRuntime against configured provider API."""

from __future__ import annotations

import os

import pytest

from emergent.agent.runtime import AgentRuntime
from emergent.config import AgentConfig, EmergentSettings

pytestmark = pytest.mark.e2e


def _require_api_key(provider: str) -> tuple[str, str, str]:
    provider_to_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    env_name = provider_to_env.get(provider)
    if env_name is None:
        return "", "", ""
    api_key = os.getenv(env_name, "").strip()
    if not api_key:
        pytest.skip(f"{env_name} is required for live e2e tests when provider={provider}")
    return env_name, api_key, provider


def _provider() -> str:
    return os.getenv("EMERGENT_E2E_PROVIDER", "anthropic").strip().lower()


@pytest.mark.asyncio
async def test_live_round_trip_text_response() -> None:
    provider = _provider()
    env_name, api_key, key_provider = _require_api_key(provider)
    settings = EmergentSettings(
        anthropic_api_key=api_key if key_provider == "anthropic" else "",
        openai_api_key=api_key if key_provider == "openai" else "",
        gemini_api_key=api_key if key_provider == "gemini" else "",
        agent=AgentConfig(
            provider=provider,
            model=os.getenv("EMERGENT_E2E_MODEL", "claude-haiku-4-5-20251001"),
            ollama_base_url=os.getenv("EMERGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            max_tokens=128,
        ),
    )
    if env_name:
        os.environ[env_name] = api_key
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
    env_name, api_key, key_provider = _require_api_key(provider)
    settings = EmergentSettings(
        anthropic_api_key=api_key if key_provider == "anthropic" else "",
        openai_api_key=api_key if key_provider == "openai" else "",
        gemini_api_key=api_key if key_provider == "gemini" else "",
        agent=AgentConfig(
            provider=provider,
            model=os.getenv("EMERGENT_E2E_MODEL", "claude-haiku-4-5-20251001"),
            ollama_base_url=os.getenv("EMERGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            max_tokens=128,
        ),
    )
    if env_name:
        os.environ[env_name] = api_key
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

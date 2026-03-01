"""Tests for provider fallback logic in AgentRuntime."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from emergent import LLMProviderError
from emergent.agent.runtime import AgentRuntime
from emergent.config import AgentConfig, EmergentSettings


def _make_settings(**overrides: object) -> EmergentSettings:
    agent_kwargs: dict = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
    }
    agent_kwargs.update(overrides)
    return EmergentSettings(
        anthropic_api_key="sk-test-key",
        openai_api_key="sk-openai-test",
        agent=AgentConfig(**agent_kwargs),
    )


def _make_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=20)
    response.content_as_dicts = MagicMock(return_value=[{"type": "text", "text": text}])
    return response


@pytest.mark.asyncio
async def test_fallback_on_provider_error() -> None:
    """When primary provider fails, fallback provider is used."""
    settings = _make_settings(fallback_providers=["openai"])
    runtime = AgentRuntime(settings=settings)

    primary_fail = AsyncMock(side_effect=LLMProviderError("primary down"))
    fallback_ok = AsyncMock(return_value=_make_text_response("from fallback"))

    # _call_with_retry will be called twice: first for primary (fail), then for fallback (ok)
    call_count = 0

    async def mock_call_with_retry(*, client, stream=False, on_text_delta=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise LLMProviderError("primary down")
        return _make_text_response("from fallback")

    try:
        with patch.object(runtime, "_call_with_retry", side_effect=mock_call_with_retry):
            text, trace = await runtime.run(
                user_message="hello",
                session_id="test-fallback",
            )
        assert text == "from fallback"
        assert trace["fallback_used"] is True
        assert trace["original_provider"] == "anthropic"
        assert trace["provider"] == "openai"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_no_fallback_when_not_configured() -> None:
    """Without fallback_providers, LLMProviderError propagates."""
    settings = _make_settings(fallback_providers=[])
    runtime = AgentRuntime(settings=settings)

    try:
        with patch.object(
            runtime,
            "_call_with_retry",
            new=AsyncMock(side_effect=LLMProviderError("provider down")),
        ):
            text, trace = await runtime.run(
                user_message="hello",
                session_id="test-no-fallback",
            )
        # LLMProviderError is caught in run() and returns error message
        assert trace["success"] is False
        assert trace["fallback_used"] is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fallback_all_fail() -> None:
    """When all fallback providers fail, original error propagates."""
    settings = _make_settings(fallback_providers=["openai"])
    runtime = AgentRuntime(settings=settings)

    try:
        with patch.object(
            runtime,
            "_call_with_retry",
            new=AsyncMock(side_effect=LLMProviderError("all providers down")),
        ):
            text, trace = await runtime.run(
                user_message="hello",
                session_id="test-all-fail",
            )
        assert trace["success"] is False
        assert "error" in (trace.get("error_message") or "").lower()
    finally:
        await runtime.close()

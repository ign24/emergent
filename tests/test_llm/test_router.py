"""Tests for LLM-as-judge prompt router."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from emergent.llm.router import PromptRouter, _CACHE_TTL_SECONDS


def _make_classify_response(tier: str) -> MagicMock:
    block = MagicMock()
    block.text = tier
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=50, output_tokens=1)
    return response


def _make_mock_client(tier: str = "DEFAULT") -> MagicMock:
    client = MagicMock()
    client.complete = AsyncMock(return_value=_make_classify_response(tier))
    return client


class TestPromptRouterClassify:
    @pytest.mark.asyncio
    async def test_short_message_fast_path(self) -> None:
        """Messages <= 8 chars bypass LLM and return FAST."""
        client = _make_mock_client()
        router = PromptRouter(client=client, model="test-model")
        result = await router.classify("hi")
        assert result.tier == "FAST"
        assert result.cached is False
        assert result.cost_usd == 0.0
        client.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_calls_llm(self) -> None:
        """Longer messages trigger LLM classification."""
        client = _make_mock_client("STRONG")
        router = PromptRouter(client=client, model="test-model")
        result = await router.classify("Analyze the architecture of this system in depth")
        assert result.tier == "STRONG"
        assert result.cached is False
        client.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_tier_defaults(self) -> None:
        """Invalid LLM output falls back to DEFAULT."""
        client = _make_mock_client("INVALID_TIER")
        router = PromptRouter(client=client, model="test-model")
        result = await router.classify("Some medium-length question here")
        assert result.tier == "DEFAULT"

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """Second call with same message returns cached result."""
        client = _make_mock_client("STRONG")
        router = PromptRouter(client=client, model="test-model")

        msg = "Analyze the architecture of this system in depth"
        r1 = await router.classify(msg)
        r2 = await router.classify(msg)

        assert r1.tier == "STRONG"
        assert r2.tier == "STRONG"
        assert r2.cached is True
        assert client.complete.call_count == 1  # Only called once

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expired cache entries trigger a new LLM call."""
        client = _make_mock_client("DEFAULT")
        router = PromptRouter(client=client, model="test-model")

        msg = "Explain how this component works in the codebase"
        await router.classify(msg)

        # Expire the cache entry
        for key in router._cache:
            tier, _ = router._cache[key]
            router._cache[key] = (tier, time.monotonic() - _CACHE_TTL_SECONDS - 1)

        result = await router.classify(msg)
        assert result.cached is False
        assert client.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_on_error(self) -> None:
        """LLM error returns DEFAULT without blocking."""
        client = MagicMock()
        client.complete = AsyncMock(side_effect=Exception("LLM unavailable"))
        router = PromptRouter(client=client, model="test-model")

        result = await router.classify("Some question that triggers an error case")
        assert result.tier == "DEFAULT"
        assert result.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_cache_eviction(self) -> None:
        """Cache evicts oldest entry when full."""
        client = _make_mock_client("DEFAULT")
        router = PromptRouter(client=client, model="test-model")

        # Fill cache beyond max
        from emergent.llm.router import _CACHE_MAX_SIZE

        for i in range(_CACHE_MAX_SIZE + 5):
            await router.classify(f"unique message number {i} with enough length")

        assert len(router._cache) <= _CACHE_MAX_SIZE

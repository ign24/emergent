"""Tests for Gemini client adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from emergent.llm.gemini_client import GeminiLLMClient


class _FakeAioModels:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def generate_content(self, **_: Any) -> Any:
        return self._response


class _FakeAio:
    def __init__(self, response: Any) -> None:
        self.models = _FakeAioModels(response)

    async def aclose(self) -> None:
        return None


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.aio = _FakeAio(response)


@pytest.mark.asyncio
async def test_complete_parses_function_calls_and_usage() -> None:
    response = SimpleNamespace(
        text="ok",
        function_calls=[SimpleNamespace(id="fn_1", name="web_fetch", args={"url": "https://x"})],
        usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=7),
    )
    client = GeminiLLMClient(api_key="gemini-test")
    client._client = _FakeClient(response)

    result = await client.complete(
        model="gemini-2.5-flash",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )

    assert result.stop_reason == "tool_use"
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 7
    assert len(result.content) == 2
    assert result.content[0].type == "tool_use"
    assert result.content[1].type == "text"


@pytest.mark.asyncio
async def test_complete_usage_defaults_to_zero() -> None:
    response = SimpleNamespace(text="ok", function_calls=[])
    client = GeminiLLMClient(api_key="gemini-test")
    client._client = _FakeClient(response)

    result = await client.complete(
        model="gemini-2.5-flash",
        system="",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )

    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0

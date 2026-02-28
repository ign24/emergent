"""Tests for OpenAI client adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from emergent.llm.openai_client import OpenAILLMClient, _to_openai_messages


class _FakeCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def create(self, **_: Any) -> Any:
        return self._response


class _FakeChat:
    def __init__(self, response: Any) -> None:
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.chat = _FakeChat(response)

    async def close(self) -> None:
        return None


def test_to_openai_messages_maps_tool_blocks() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "file_read", "input": {"path": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"}],
        },
    ]

    converted = _to_openai_messages("sys", messages)
    assert converted[0]["role"] == "system"
    assert converted[2]["role"] == "assistant"
    assert converted[2]["tool_calls"][0]["function"]["name"] == "file_read"
    assert converted[3]["role"] == "tool"
    assert converted[3]["tool_call_id"] == "tool_1"


@pytest.mark.asyncio
async def test_complete_parses_text_and_tool_call() -> None:
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="search_files", arguments='{"pattern":"*.py"}'),
    )
    message = SimpleNamespace(content="done", tool_calls=[tool_call])
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
    )

    client = OpenAILLMClient(api_key="sk-test")
    client._client = _FakeClient(response)

    result = await client.complete(
        model="gpt-5-codex",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
    )

    assert result.stop_reason == "tool_use"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 8
    assert len(result.content) == 2
    assert result.content[0].type == "tool_use"
    assert result.content[1].type == "text"

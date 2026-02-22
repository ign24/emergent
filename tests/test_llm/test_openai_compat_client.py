from __future__ import annotations

import json
from typing import Any

import pytest

from emergent.llm.openai_compat_client import OpenAICompatibleLLMClient


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHTTPClient:
    def __init__(self, response_data: dict[str, Any]) -> None:
        self.response_data = response_data
        self.last_path = ""
        self.last_json: dict[str, Any] = {}

    async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:  # noqa: A002
        self.last_path = path
        self.last_json = json
        return _FakeResponse(self.response_data)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_complete_maps_tool_calls_and_usage() -> None:
    client = OpenAICompatibleLLMClient(api_key="k", base_url="https://openrouter.ai/api/v1")
    fake = _FakeHTTPClient(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "shell_execute",
                                    "arguments": json.dumps({"command": "ls"}),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    client._client = fake  # type: ignore[assignment]

    response = await client.complete(
        model="openai/gpt-5-mini",
        system="system",
        messages=[{"role": "user", "content": "hola"}],
        max_tokens=100,
        tools=[
            {
                "name": "shell_execute",
                "description": "run shell",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
    )

    assert fake.last_path == "/chat/completions"
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert len(response.content) == 1
    assert getattr(response.content[0], "name", "") == "shell_execute"


@pytest.mark.asyncio
async def test_complete_translates_history_tool_messages() -> None:
    client = OpenAICompatibleLLMClient(api_key="k", base_url="https://openrouter.ai/api/v1")
    fake = _FakeHTTPClient(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "ok",
                        "tool_calls": [],
                    },
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }
    )
    client._client = fake  # type: ignore[assignment]

    await client.complete(
        model="openai/gpt-5-mini",
        system="sys",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_123",
                        "name": "shell_execute",
                        "input": {"command": "ls"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_123",
                        "content": "file1",
                    }
                ],
            },
        ],
        max_tokens=32,
    )

    payload_messages = fake.last_json["messages"]
    assert payload_messages[0]["role"] == "system"
    assert payload_messages[1]["role"] == "assistant"
    assert payload_messages[1]["tool_calls"][0]["id"] == "tool_123"
    assert payload_messages[2]["role"] == "tool"
    assert payload_messages[2]["tool_call_id"] == "tool_123"

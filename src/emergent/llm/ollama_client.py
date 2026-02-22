"""Ollama adapter implementing the provider-agnostic LLM client interface."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from emergent import LLMProviderError, LLMRetryableError
from emergent.llm.client import LLMClient
from emergent.llm.models import LLMResponse, LLMTextBlock, LLMToolUseBlock, LLMUsage


class OllamaLLMClient(LLMClient):
    """Adapter over Ollama /api/chat using non-streaming responses."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload_messages: list[dict[str, Any]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]

        try:
            response = await self._client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise LLMRetryableError(str(e)) from e
        except httpx.HTTPError as e:
            raise LLMProviderError(str(e)) from e
        except ValueError as e:
            raise LLMProviderError(f"Invalid JSON from Ollama: {e}") from e

        message = data.get("message") or {}
        text = str(message.get("content") or "")
        raw_tool_calls = message.get("tool_calls") or []

        content: list[LLMTextBlock | LLMToolUseBlock] = []
        for call in raw_tool_calls:
            function = call.get("function") or {}
            content.append(
                LLMToolUseBlock(
                    type="tool_use",
                    id=str(call.get("id") or uuid.uuid4().hex[:12]),
                    name=str(function.get("name") or ""),
                    input=dict(function.get("arguments") or {}),
                )
            )

        if text:
            content.append(LLMTextBlock(type="text", text=text))

        stop_reason = "tool_use" if raw_tool_calls else "end_turn"

        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=LLMUsage(
                input_tokens=int(data.get("prompt_eval_count") or 0),
                output_tokens=int(data.get("eval_count") or 0),
            ),
        )

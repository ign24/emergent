"""Anthropic adapter implementing the provider-agnostic LLM client interface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic

from emergent import LLMProviderError, LLMRetryableError
from emergent.llm.client import LLMClient
from emergent.llm.models import LLMResponse, LLMTextBlock, LLMToolUseBlock, LLMUsage


class AnthropicLLMClient(LLMClient):
    """Adapter over AsyncAnthropic.messages.create."""

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def close(self) -> None:
        await self._client.close()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            response = await self._client.messages.create(**kwargs)
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APITimeoutError,
        ) as e:
            raise LLMRetryableError(str(e)) from e
        except anthropic.APIError as e:
            raise LLMProviderError(str(e)) from e

        content: list[LLMTextBlock | LLMToolUseBlock] = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                content.append(LLMTextBlock(type="text", text=getattr(block, "text", "")))
            elif getattr(block, "type", "") == "tool_use":
                content.append(
                    LLMToolUseBlock(
                        type="tool_use",
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=dict(getattr(block, "input", {}) or {}),
                    )
                )

        return LLMResponse(
            stop_reason=str(response.stop_reason),
            content=content,
            usage=LLMUsage(
                input_tokens=int(response.usage.input_tokens),
                output_tokens=int(response.usage.output_tokens),
            ),
        )

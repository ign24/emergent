"""OpenAI adapter implementing the provider-agnostic LLM client interface."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from emergent import LLMProviderError, LLMRetryableError
from emergent.llm.client import LLMClient, TextDeltaCallback
from emergent.llm.models import LLMResponse, LLMTextBlock, LLMToolUseBlock, LLMUsage


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "parameters": dict(tool.get("input_schema") or {}),
            },
        }
        for tool in tools
    ]


def _to_openai_messages(system: str, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    if system.strip():
        payload.append({"role": "system", "content": system})

    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")

        if role == "user":
            if isinstance(content, str):
                payload.append({"role": "user", "content": content})
                continue
            if isinstance(content, list):
                text_parts: list[str] = []
                saw_tool_result = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "")
                    if block_type == "tool_result":
                        saw_tool_result = True
                        payload.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(block.get("tool_use_id") or ""),
                                "content": str(block.get("content") or ""),
                            }
                        )
                    elif block_type == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            text_parts.append(text)

                if text_parts or not saw_tool_result:
                    payload.append({"role": "user", "content": "\n".join(text_parts)})
                continue

            payload.append({"role": "user", "content": str(content or "")})
            continue

        if role == "assistant":
            if isinstance(content, str):
                payload.append({"role": "assistant", "content": content})
                continue
            if isinstance(content, list):
                assistant_text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "")
                    if block_type == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            assistant_text_parts.append(text)
                    elif block_type == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or uuid.uuid4().hex[:12]),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name") or ""),
                                    "arguments": json.dumps(dict(block.get("input") or {})),
                                },
                            }
                        )

                if tool_calls:
                    payload.append(
                        {
                            "role": "assistant",
                            "content": "\n".join(assistant_text_parts),
                            "tool_calls": tool_calls,
                        }
                    )
                elif assistant_text_parts:
                    payload.append(
                        {"role": "assistant", "content": "\n".join(assistant_text_parts)}
                    )
                continue

            payload.append({"role": "assistant", "content": str(content or "")})
            continue

        if role == "system":
            payload.append({"role": "system", "content": str(content or "")})

    return payload


class OpenAILLMClient(LLMClient):
    """Adapter over AsyncOpenAI chat completions."""

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

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
        params: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            params["tools"] = _to_openai_tools(tools)
            params["tool_choice"] = "auto"

        try:
            response = await self._client.chat.completions.create(**params)
        except (RateLimitError, InternalServerError, APITimeoutError, APIConnectionError) as e:
            raise LLMRetryableError(str(e)) from e
        except APIError as e:
            raise LLMProviderError(str(e)) from e

        if not response.choices:
            raise LLMProviderError("OpenAI response has no choices")

        message = response.choices[0].message
        content: list[LLMTextBlock | LLMToolUseBlock] = []

        raw_tool_calls = message.tool_calls or []
        for tool_call in raw_tool_calls:
            fn = getattr(tool_call, "function", None)
            raw_args = str(getattr(fn, "arguments", "") or "{}")
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                parsed_args = {}

            content.append(
                LLMToolUseBlock(
                    type="tool_use",
                    id=str(getattr(tool_call, "id", "") or uuid.uuid4().hex[:12]),
                    name=str(getattr(fn, "name", "") or ""),
                    input=parsed_args,
                )
            )

        text = str(message.content or "")
        if text:
            content.append(LLMTextBlock(type="text", text=text))

        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        stop_reason = "tool_use" if raw_tool_calls else "end_turn"
        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )

    async def stream_complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        on_text_delta: TextDeltaCallback,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        response = await self.complete(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
        )

        for block in response.content:
            if isinstance(block, LLMTextBlock) and block.text:
                await on_text_delta(block.text)

        return response

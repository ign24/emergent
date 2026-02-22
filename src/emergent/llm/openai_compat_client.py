"""OpenAI-compatible chat completions adapter (OpenRouter, OpenAI-like APIs)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from emergent import LLMProviderError, LLMRetryableError
from emergent.llm.client import LLMClient
from emergent.llm.models import LLMResponse, LLMTextBlock, LLMToolUseBlock, LLMUsage


class OpenAICompatibleLLMClient(LLMClient):
    """Adapter over /chat/completions for OpenAI-compatible APIs."""

    def __init__(self, api_key: str, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(system=system, messages=messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [_to_openai_tool(t) for t in tools]

        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise LLMRetryableError(str(e)) from e
        except httpx.HTTPError as e:
            raise LLMProviderError(str(e)) from e
        except ValueError as e:
            raise LLMProviderError(f"Invalid JSON from OpenAI-compatible provider: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError("No choices returned by OpenAI-compatible provider")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = str(choice.get("finish_reason") or "stop")

        content: list[LLMTextBlock | LLMToolUseBlock] = []
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            content.append(LLMTextBlock(type="text", text=text))

        raw_tool_calls = message.get("tool_calls") or []
        for call in raw_tool_calls:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            parsed_input: dict[str, Any] = {}
            if isinstance(arguments, str) and arguments.strip():
                try:
                    loaded = json.loads(arguments)
                    if isinstance(loaded, dict):
                        parsed_input = loaded
                except json.JSONDecodeError:
                    parsed_input = {}
            content.append(
                LLMToolUseBlock(
                    type="tool_use",
                    id=str(call.get("id") or uuid.uuid4().hex[:12]),
                    name=str(function.get("name") or ""),
                    input=parsed_input,
                )
            )

        stop_reason = "tool_use" if finish_reason in {"tool_calls", "function_call"} else "end_turn"
        usage = data.get("usage") or {}

        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=LLMUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _to_openai_messages(*, system: str, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_messages: list[dict[str, Any]] = []
    if system:
        payload_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if isinstance(content, str):
            payload_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            payload_messages.append({"role": role, "content": str(content or "")})
            continue

        # Anthropic assistant tool_use -> OpenAI assistant tool_calls
        if role == "assistant":
            assistant_text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        assistant_text_parts.append(text)
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(block.get("id") or uuid.uuid4().hex[:12]),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name") or ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        }
                    )

            assistant_payload: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(assistant_text_parts) if assistant_text_parts else "",
            }
            if tool_calls:
                assistant_payload["tool_calls"] = tool_calls
            payload_messages.append(assistant_payload)
            continue

        # Anthropic user tool_result -> OpenAI tool messages
        if role == "user":
            emitted_tool_message = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                payload_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": str(block.get("content") or ""),
                    }
                )
                emitted_tool_message = True
            if not emitted_tool_message:
                payload_messages.append({"role": role, "content": ""})
            continue

        payload_messages.append({"role": role, "content": ""})

    return payload_messages

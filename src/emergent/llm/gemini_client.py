"""Gemini adapter implementing the provider-agnostic LLM client interface."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Sequence
from typing import Any, cast

from google import genai
from google.genai import types

from emergent import LLMProviderError, LLMRetryableError
from emergent.llm.client import LLMClient, TextDeltaCallback
from emergent.llm.models import LLMResponse, LLMTextBlock, LLMToolUseBlock, LLMUsage


def _normalize_gemini_error(error: Exception) -> Exception:
    text = str(error).lower()
    if any(token in text for token in ("rate limit", "429", "timeout", "tempor", "unavailable")):
        return LLMRetryableError(str(error))
    return LLMProviderError(str(error))


def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[types.Tool]:
    declarations: list[types.FunctionDeclaration] = []
    for tool in tools:
        declarations.append(
            types.FunctionDeclaration(
                name=str(tool.get("name") or ""),
                description=str(tool.get("description") or ""),
                parameters_json_schema=dict(tool.get("input_schema") or {}),
            )
        )
    if not declarations:
        return []
    return [types.Tool(function_declarations=declarations)]


def _message_to_text(role: str, content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            if text:
                lines.append(text)
        elif block_type == "tool_use":
            name = str(block.get("name") or "")
            args = dict(block.get("input") or {})
            lines.append(f"tool_call:{name} args={json.dumps(args, ensure_ascii=True)}")
        elif block_type == "tool_result":
            tool_id = str(block.get("tool_use_id") or "")
            result = str(block.get("content") or "")
            lines.append(f"tool_result:{tool_id} content={result}")

    if lines:
        return "\n".join(lines)
    return f"[{role}]"


def _to_gemini_contents(messages: Sequence[dict[str, Any]]) -> list[types.Content]:
    contents: list[types.Content] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant", "system"}:
            continue

        text = _message_to_text(role, message.get("content"))
        if not text.strip():
            continue

        gemini_role = "model" if role == "assistant" else "user"
        contents.append(types.Content(role=gemini_role, parts=[types.Part.from_text(text=text)]))
    return contents


class GeminiLLMClient(LLMClient):
    """Adapter over Google Gen AI async generate_content."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def close(self) -> None:
        return None

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        contents = _to_gemini_contents(messages)
        if system.strip():
            contents.insert(
                0, types.Content(role="user", parts=[types.Part.from_text(text=system)])
            )

        config = types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            tools=cast(Any, _to_gemini_tools(tools) if tools else None),
        )

        try:
            response = await cast(
                Awaitable[Any],
                self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                ),
            )
        except Exception as e:  # pragma: no cover - provider exception types vary by SDK versions
            raise _normalize_gemini_error(e) from e

        content: list[LLMTextBlock | LLMToolUseBlock] = []
        raw_calls = list(getattr(response, "function_calls", None) or [])

        for call in raw_calls:
            raw_args = getattr(call, "args", {})
            if not isinstance(raw_args, dict):
                raw_args = {}
            content.append(
                LLMToolUseBlock(
                    type="tool_use",
                    id=str(getattr(call, "id", "") or uuid.uuid4().hex[:12]),
                    name=str(getattr(call, "name", "") or ""),
                    input=raw_args,
                )
            )

        text = str(getattr(response, "text", "") or "")
        if text:
            content.append(LLMTextBlock(type="text", text=text))

        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(
            getattr(usage, "candidates_token_count", 0)
            or getattr(usage, "output_token_count", 0)
            or 0
        )

        stop_reason = "tool_use" if raw_calls else "end_turn"
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

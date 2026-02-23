"""Provider-agnostic async LLM client interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from emergent.llm.models import LLMResponse

TextDeltaCallback = Callable[[str], Awaitable[None]]


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...

    async def stream_complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        max_tokens: int,
        on_text_delta: TextDeltaCallback,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...

    async def close(self) -> None: ...

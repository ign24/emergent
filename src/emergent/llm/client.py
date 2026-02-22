"""Provider-agnostic async LLM client interface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from emergent.llm.models import LLMResponse


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

    async def close(self) -> None: ...

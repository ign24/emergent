"""Provider-agnostic LLM message and response models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMTextBlock:
    type: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LLMToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LLMContentBlock = LLMTextBlock | LLMToolUseBlock


@dataclass
class LLMResponse:
    stop_reason: str
    content: list[LLMContentBlock]
    usage: LLMUsage

    def content_as_dicts(self) -> list[dict[str, Any]]:
        """Return content blocks as plain dicts for message history."""
        return [block.to_dict() for block in self.content]

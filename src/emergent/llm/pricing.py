"""Cross-provider model pricing and cost calculation.

Centralizes cost tracking for all supported LLM providers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing in USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    provider: str


# Pricing table — update when providers change rates.
# Ollama models are local and free (cost = 0.0).
MODEL_PRICING: dict[str, ModelPricing] = {
    # Anthropic
    "claude-sonnet-4-20250514": ModelPricing(3.00, 15.00, "anthropic"),
    "claude-haiku-4-5-20251001": ModelPricing(0.80, 4.00, "anthropic"),
    # OpenAI
    "gpt-4o": ModelPricing(2.50, 10.00, "openai"),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, "openai"),
    "gpt-4.1": ModelPricing(2.00, 8.00, "openai"),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60, "openai"),
    "gpt-4.1-nano": ModelPricing(0.10, 0.40, "openai"),
    # Gemini
    "gemini-2.0-flash": ModelPricing(0.10, 0.40, "gemini"),
    "gemini-2.5-pro": ModelPricing(1.25, 10.00, "gemini"),
    "gemini-2.5-flash": ModelPricing(0.15, 0.60, "gemini"),
}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a given model and token counts.

    Returns 0.0 for unknown models (including all Ollama models).
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return 0.0
    return (
        input_tokens * pricing.input_per_mtok + output_tokens * pricing.output_per_mtok
    ) / 1_000_000

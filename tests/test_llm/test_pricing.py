"""Tests for cross-provider pricing module."""

from __future__ import annotations

import pytest

from emergent.llm.pricing import MODEL_PRICING, ModelPricing, calculate_cost


class TestModelPricing:
    def test_anthropic_sonnet_in_table(self) -> None:
        p = MODEL_PRICING["claude-sonnet-4-20250514"]
        assert p.provider == "anthropic"
        assert p.input_per_mtok == 3.00
        assert p.output_per_mtok == 15.00

    def test_anthropic_haiku_in_table(self) -> None:
        p = MODEL_PRICING["claude-haiku-4-5-20251001"]
        assert p.provider == "anthropic"
        assert p.input_per_mtok == 0.80

    def test_openai_models_in_table(self) -> None:
        for model in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"):
            assert model in MODEL_PRICING
            assert MODEL_PRICING[model].provider == "openai"

    def test_gemini_models_in_table(self) -> None:
        for model in ("gemini-2.0-flash", "gemini-2.5-pro", "gemini-2.5-flash"):
            assert model in MODEL_PRICING
            assert MODEL_PRICING[model].provider == "gemini"

    def test_pricing_dataclass_frozen(self) -> None:
        p = ModelPricing(1.0, 2.0, "test")
        with pytest.raises(AttributeError):
            p.input_per_mtok = 5.0  # type: ignore[misc]


class TestCalculateCost:
    def test_known_model_cost(self) -> None:
        # claude-sonnet: $3.00 input, $15.00 output per M tokens
        cost = calculate_cost("claude-sonnet-4-20250514", 1000, 500)
        expected = (1000 * 3.00 + 500 * 15.00) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_unknown_model_returns_zero(self) -> None:
        assert calculate_cost("unknown-model", 1000, 500) == 0.0

    def test_ollama_returns_zero(self) -> None:
        assert calculate_cost("llama3", 10000, 5000) == 0.0

    def test_zero_tokens(self) -> None:
        assert calculate_cost("claude-sonnet-4-20250514", 0, 0) == 0.0

    def test_openai_cost(self) -> None:
        # gpt-4o: $2.50 input, $10.00 output per M tokens
        cost = calculate_cost("gpt-4o", 2000, 1000)
        expected = (2000 * 2.50 + 1000 * 10.00) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_gemini_cost(self) -> None:
        cost = calculate_cost("gemini-2.0-flash", 5000, 2000)
        expected = (5000 * 0.10 + 2000 * 0.40) / 1_000_000
        assert cost == pytest.approx(expected)

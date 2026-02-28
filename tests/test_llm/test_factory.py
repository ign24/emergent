"""Tests for LLM client factory provider selection."""

from __future__ import annotations

import pytest

from emergent import ConfigurationError
from emergent.config import EmergentSettings
from emergent.llm.anthropic_client import AnthropicLLMClient
from emergent.llm.factory import create_llm_client
from emergent.llm.gemini_client import GeminiLLMClient
from emergent.llm.ollama_client import OllamaLLMClient
from emergent.llm.openai_client import OpenAILLMClient


def test_factory_anthropic_requires_key() -> None:
    settings = EmergentSettings(anthropic_api_key="")
    with pytest.raises(ConfigurationError):
        create_llm_client(settings, "anthropic")


def test_factory_openai_requires_key() -> None:
    settings = EmergentSettings(openai_api_key="")
    with pytest.raises(ConfigurationError):
        create_llm_client(settings, "openai")


def test_factory_gemini_requires_key() -> None:
    settings = EmergentSettings(gemini_api_key="")
    with pytest.raises(ConfigurationError):
        create_llm_client(settings, "gemini")


def test_factory_unsupported_provider() -> None:
    settings = EmergentSettings()
    with pytest.raises(ConfigurationError):
        create_llm_client(settings, "unknown")


@pytest.mark.asyncio
async def test_factory_returns_concrete_clients() -> None:
    settings = EmergentSettings(
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-openai-test",
        gemini_api_key="gemini-test-key",
    )

    anthropic_client = create_llm_client(settings, "anthropic")
    openai_client = create_llm_client(settings, "openai")
    gemini_client = create_llm_client(settings, "gemini")
    ollama_client = create_llm_client(settings, "ollama")

    assert isinstance(anthropic_client, AnthropicLLMClient)
    assert isinstance(openai_client, OpenAILLMClient)
    assert isinstance(gemini_client, GeminiLLMClient)
    assert isinstance(ollama_client, OllamaLLMClient)

    await anthropic_client.close()
    await openai_client.close()
    await gemini_client.close()
    await ollama_client.close()

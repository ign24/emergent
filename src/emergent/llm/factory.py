"""Factory for model-provider clients."""

from __future__ import annotations

from emergent import ConfigurationError
from emergent.config import EmergentSettings
from emergent.llm.anthropic_client import AnthropicLLMClient
from emergent.llm.client import LLMClient
from emergent.llm.gemini_client import GeminiLLMClient
from emergent.llm.ollama_client import OllamaLLMClient
from emergent.llm.openai_client import OpenAILLMClient


def create_llm_client(settings: EmergentSettings, provider: str) -> LLMClient:
    p = provider.strip().lower()
    if p == "anthropic":
        if not settings.anthropic_api_key:
            raise ConfigurationError("ANTHROPIC_API_KEY is required when provider=anthropic")
        return AnthropicLLMClient(api_key=settings.anthropic_api_key)
    if p == "openai":
        if not settings.openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required when provider=openai")
        return OpenAILLMClient(api_key=settings.openai_api_key)
    if p == "gemini":
        if not settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is required when provider=gemini")
        return GeminiLLMClient(api_key=settings.gemini_api_key)
    if p == "ollama":
        return OllamaLLMClient(base_url=settings.agent.ollama_base_url)
    raise ConfigurationError(f"Unsupported LLM provider: {provider}")

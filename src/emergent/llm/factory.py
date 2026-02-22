"""Factory for model-provider clients."""

from __future__ import annotations

from emergent import ConfigurationError
from emergent.config import EmergentSettings
from emergent.llm.anthropic_client import AnthropicLLMClient
from emergent.llm.client import LLMClient
from emergent.llm.ollama_client import OllamaLLMClient
from emergent.llm.openai_compat_client import OpenAICompatibleLLMClient


def create_llm_client(settings: EmergentSettings, provider: str) -> LLMClient:
    p = provider.strip().lower()
    provider_cfg = settings.agent.get_provider(p)

    if p == "anthropic":
        api_key = settings.get_provider_api_key(p, provider_cfg)
        if not api_key:
            raise ConfigurationError("ANTHROPIC_API_KEY is required when provider=anthropic")
        return AnthropicLLMClient(api_key=api_key)

    if p == "openrouter":
        api_key = settings.get_provider_api_key(p, provider_cfg)
        if not api_key:
            raise ConfigurationError("OPENROUTER_API_KEY is required when provider=openrouter")
        base_url = provider_cfg.base_url or "https://openrouter.ai/api/v1"
        return OpenAICompatibleLLMClient(api_key=api_key, base_url=base_url)

    if p == "ollama":
        base_url = provider_cfg.base_url or settings.agent.ollama_base_url
        return OllamaLLMClient(base_url=base_url)

    raise ConfigurationError(f"Unsupported LLM provider: {provider}")

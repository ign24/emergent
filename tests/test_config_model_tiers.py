from __future__ import annotations

from emergent.config import AgentConfig, ModelTier, ModelTierConfig, ProviderConfig


def test_agent_config_backward_compatible_properties() -> None:
    agent = AgentConfig(
        providers={
            "anthropic": ProviderConfig(api_key_env="ANTHROPIC_API_KEY"),
            "ollama": ProviderConfig(base_url="http://127.0.0.1:11434"),
        },
        models={
            "default": ModelTierConfig(
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
            ),
            "summary": ModelTierConfig(
                provider="ollama",
                model="qwen2.5:7b",
                max_tokens=300,
            ),
        },
    )

    assert agent.provider == "anthropic"
    assert agent.model == "claude-sonnet-4-20250514"
    assert agent.max_tokens == 1024
    assert agent.summary_provider == "ollama"
    assert agent.haiku_model == "qwen2.5:7b"
    assert agent.ollama_base_url == "http://127.0.0.1:11434"


def test_agent_config_tier_fallback_to_default() -> None:
    agent = AgentConfig(
        models={
            "default": ModelTierConfig(
                provider="anthropic", model="claude-haiku-4-5", max_tokens=512
            )
        }
    )

    selected = agent.get_tier(ModelTier.STRONG)
    assert selected.model == "claude-haiku-4-5"

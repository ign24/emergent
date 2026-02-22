"""Configuration loading via pydantic-settings.

Loads from .env (secrets) and config.yaml (runtime settings).
Supports model tiers and multiple LLM providers with legacy compatibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelTier(StrEnum):
    FAST = "fast"
    DEFAULT = "default"
    STRONG = "strong"
    SUMMARY = "summary"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class AgentConfig:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelTierConfig] = field(default_factory=dict)
    routing_enabled: bool = True
    data_dir: str = "./data"

    # Hardcoded guards — NOT overridable by the agent at runtime
    MAX_ITERATIONS: int = 15
    MAX_TOKENS_SESSION: int = 100_000
    TIMEOUT_PER_TOOL_SECONDS: int = 30
    TIMEOUT_SESSION_SECONDS: int = 300
    MAX_TOOL_OUTPUT_CHARS: int = 10_000
    CONFIRMATION_TIMEOUT_SECONDS: int = 60

    # ------------------------------------------------------------------
    # Backward-compatible convenience accessors
    # ------------------------------------------------------------------
    @property
    def provider(self) -> str:
        return self.default_model.provider

    @property
    def model(self) -> str:
        return self.default_model.model

    @property
    def max_tokens(self) -> int:
        return self.default_model.max_tokens

    @property
    def haiku_model(self) -> str:
        return self.summary_model.model

    @property
    def summary_provider(self) -> str:
        return self.summary_model.provider

    @property
    def ollama_base_url(self) -> str:
        ollama = self.providers.get("ollama")
        return ollama.base_url if ollama and ollama.base_url else "http://127.0.0.1:11434"

    @property
    def default_model(self) -> ModelTierConfig:
        return self.get_tier(ModelTier.DEFAULT)

    @property
    def summary_model(self) -> ModelTierConfig:
        return self.get_tier(ModelTier.SUMMARY)

    def get_tier(self, tier: ModelTier | str) -> ModelTierConfig:
        tier_key = tier.value if isinstance(tier, ModelTier) else str(tier)
        if tier_key in self.models:
            return self.models[tier_key]
        if ModelTier.DEFAULT.value in self.models:
            return self.models[ModelTier.DEFAULT.value]
        raise ValueError(f"Missing model tier configuration: {tier_key}")

    def get_provider(self, provider_name: str) -> ProviderConfig:
        return self.providers.get(provider_name, ProviderConfig())


@dataclass
class ProviderConfig:
    api_key_env: str = ""
    base_url: str = ""


@dataclass
class ModelTierConfig:
    provider: str
    model: str
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    max_tokens: int = 4096


class _EnvSettings(BaseSettings):
    """Loads raw values from .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ANTHROPIC_API_KEY: str = Field(default="")
    OPENROUTER_API_KEY: str = Field(default="")
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_ALLOWED_USER_IDS: str = Field(default="")

    # Optional overrides
    EMERGENT_PROVIDER: str = Field(default="")
    EMERGENT_MODEL: str = Field(default="")
    EMERGENT_HAIKU_MODEL: str = Field(default="")
    EMERGENT_SUMMARY_PROVIDER: str = Field(default="")
    EMERGENT_OLLAMA_BASE_URL: str = Field(default="")
    EMERGENT_DATA_DIR: str = Field(default="")


@dataclass
class EmergentSettings:
    """Assembled settings from .env + config.yaml."""

    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    system_prompt: str = ""
    memory: dict[str, Any] = field(default_factory=dict)
    observability: dict[str, Any] = field(default_factory=dict)
    tools_config: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)

    def get_provider_api_key(self, provider: str, provider_cfg: ProviderConfig) -> str:
        normalized = provider.strip().lower()
        if normalized == "anthropic":
            return self.anthropic_api_key
        if normalized == "openrouter":
            return self.openrouter_api_key

        env_var = provider_cfg.api_key_env.strip()
        if env_var:
            return os.getenv(env_var, "")
        return ""


def _parse_user_ids(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _model_default_pricing(model: str) -> tuple[float, float]:
    model_id = model.strip().lower()
    known: dict[str, tuple[float, float]] = {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-sonnet-4-5-20250929": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
    }
    if model_id in known:
        return known[model_id]
    for key, value in known.items():
        if model_id.startswith(key):
            return value
    return (0.0, 0.0)


def _build_tier(
    *,
    provider: str,
    model: str,
    max_tokens: int,
    input_per_mtok: float | None,
    output_per_mtok: float | None,
) -> ModelTierConfig:
    default_input, default_output = _model_default_pricing(model)
    return ModelTierConfig(
        provider=provider,
        model=model,
        input_per_mtok=default_input if input_per_mtok is None else float(input_per_mtok),
        output_per_mtok=default_output if output_per_mtok is None else float(output_per_mtok),
        max_tokens=int(max_tokens),
    )


def _build_model_tiers(
    *,
    env: _EnvSettings,
    agent_yaml: dict[str, Any],
    default_provider: str,
    default_model: str,
    summary_provider: str,
    summary_model: str,
    default_max_tokens: int,
) -> dict[str, ModelTierConfig]:
    models_yaml = agent_yaml.get("models", {}) or {}
    if isinstance(models_yaml, dict) and models_yaml:
        parsed: dict[str, ModelTierConfig] = {}
        for tier_name, raw_cfg in models_yaml.items():
            if not isinstance(raw_cfg, dict):
                continue
            provider = str(raw_cfg.get("provider", default_provider))
            model = str(raw_cfg.get("model", default_model))
            parsed[str(tier_name)] = _build_tier(
                provider=provider,
                model=model,
                max_tokens=int(raw_cfg.get("max_tokens", default_max_tokens)),
                input_per_mtok=raw_cfg.get("input_per_mtok"),
                output_per_mtok=raw_cfg.get("output_per_mtok"),
            )

        if parsed:
            parsed.setdefault(
                ModelTier.DEFAULT.value,
                _build_tier(
                    provider=default_provider,
                    model=default_model,
                    max_tokens=default_max_tokens,
                    input_per_mtok=None,
                    output_per_mtok=None,
                ),
            )
            parsed.setdefault(
                ModelTier.SUMMARY.value,
                _build_tier(
                    provider=summary_provider,
                    model=summary_model,
                    max_tokens=min(default_max_tokens, 300),
                    input_per_mtok=None,
                    output_per_mtok=None,
                ),
            )
            parsed.setdefault(ModelTier.FAST.value, parsed[ModelTier.DEFAULT.value])
            parsed.setdefault(ModelTier.STRONG.value, parsed[ModelTier.DEFAULT.value])
            return parsed

    # Legacy fallback
    return {
        ModelTier.FAST.value: _build_tier(
            provider=summary_provider,
            model=summary_model,
            max_tokens=min(default_max_tokens, 2048),
            input_per_mtok=None,
            output_per_mtok=None,
        ),
        ModelTier.DEFAULT.value: _build_tier(
            provider=default_provider,
            model=default_model,
            max_tokens=default_max_tokens,
            input_per_mtok=None,
            output_per_mtok=None,
        ),
        ModelTier.STRONG.value: _build_tier(
            provider=default_provider,
            model=default_model,
            max_tokens=max(default_max_tokens, 4096),
            input_per_mtok=None,
            output_per_mtok=None,
        ),
        ModelTier.SUMMARY.value: _build_tier(
            provider=summary_provider,
            model=summary_model,
            max_tokens=300,
            input_per_mtok=None,
            output_per_mtok=None,
        ),
    }


def _build_providers(env: _EnvSettings, agent_yaml: dict[str, Any]) -> dict[str, ProviderConfig]:
    providers_yaml = agent_yaml.get("providers", {}) or {}
    if not isinstance(providers_yaml, dict):
        providers_yaml = {}

    legacy_ollama = env.EMERGENT_OLLAMA_BASE_URL or agent_yaml.get(
        "ollama_base_url", "http://127.0.0.1:11434"
    )
    ollama_cfg = (
        providers_yaml.get("ollama", {}) if isinstance(providers_yaml.get("ollama"), dict) else {}
    )

    def _provider_cfg(
        name: str, *, default_env: str = "", default_base: str = ""
    ) -> ProviderConfig:
        raw = providers_yaml.get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        return ProviderConfig(
            api_key_env=str(raw.get("api_key_env", default_env)),
            base_url=str(raw.get("base_url", default_base)),
        )

    providers: dict[str, ProviderConfig] = {
        "anthropic": _provider_cfg("anthropic", default_env="ANTHROPIC_API_KEY"),
        "openrouter": _provider_cfg("openrouter", default_env="OPENROUTER_API_KEY"),
        "ollama": ProviderConfig(
            api_key_env=str(ollama_cfg.get("api_key_env", "")),
            base_url=str(ollama_cfg.get("base_url", legacy_ollama)),
        ),
    }
    return providers


def _load_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> EmergentSettings:
    """Load and cache settings. Called once at startup."""
    env = _EnvSettings()
    yaml_cfg = _load_yaml_config(Path.cwd() / "config.yaml")

    agent_yaml = yaml_cfg.get("agent", {})

    telegram = TelegramConfig(
        bot_token=env.TELEGRAM_BOT_TOKEN,
        allowed_user_ids=_parse_user_ids(env.TELEGRAM_ALLOWED_USER_IDS),
    )

    default_provider = str(env.EMERGENT_PROVIDER or agent_yaml.get("provider", "anthropic"))
    default_model = str(env.EMERGENT_MODEL or agent_yaml.get("model", "claude-sonnet-4-20250514"))
    summary_provider = str(
        env.EMERGENT_SUMMARY_PROVIDER or agent_yaml.get("summary_provider", "anthropic")
    )
    summary_model = str(
        env.EMERGENT_HAIKU_MODEL or agent_yaml.get("haiku_model", "claude-haiku-4-5-20251001")
    )
    default_max_tokens = int(agent_yaml.get("max_tokens", 4096))

    providers = _build_providers(env, agent_yaml)
    models = _build_model_tiers(
        env=env,
        agent_yaml=agent_yaml,
        default_provider=default_provider,
        default_model=default_model,
        summary_provider=summary_provider,
        summary_model=summary_model,
        default_max_tokens=default_max_tokens,
    )

    agent = AgentConfig(
        providers=providers,
        models=models,
        routing_enabled=bool(agent_yaml.get("routing_enabled", True)),
        data_dir=env.EMERGENT_DATA_DIR or agent_yaml.get("data_dir", "./data"),
    )

    settings = EmergentSettings(
        anthropic_api_key=env.ANTHROPIC_API_KEY,
        openrouter_api_key=env.OPENROUTER_API_KEY,
        telegram=telegram,
        agent=agent,
        system_prompt=yaml_cfg.get("system_prompt", ""),
        memory=yaml_cfg.get("memory", {}),
        observability=yaml_cfg.get("observability", {}),
        tools_config=yaml_cfg.get("tools", {}),
        voice=yaml_cfg.get("voice", {}),
    )

    # Make API key available as env var for the anthropic client
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.openrouter_api_key:
        os.environ["OPENROUTER_API_KEY"] = settings.openrouter_api_key

    return settings


def verify_guards_integrity(settings: EmergentSettings) -> None:
    """Verify hardcoded guards haven't been tampered with. Called at startup."""
    g = settings.agent
    assert g.MAX_ITERATIONS == 15, "Guard violation: MAX_ITERATIONS"
    assert g.MAX_TOKENS_SESSION == 100_000, "Guard violation: MAX_TOKENS_SESSION"
    assert g.TIMEOUT_PER_TOOL_SECONDS == 30, "Guard violation: TIMEOUT_PER_TOOL"
    assert g.TIMEOUT_SESSION_SECONDS == 300, "Guard violation: TIMEOUT_SESSION"
    assert g.MAX_TOOL_OUTPUT_CHARS == 10_000, "Guard violation: MAX_TOOL_OUTPUT"
    assert g.CONFIRMATION_TIMEOUT_SECONDS == 60, "Guard violation: CONFIRMATION_TIMEOUT"

"""Configuration loading via pydantic-settings.

Loads from .env (secrets) and config.yaml (runtime settings).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class ResearchConfig:
    enabled: bool = True
    schedule: str = "30 9 * * *"
    max_findings_per_domain: int = 10
    highlight_threshold: float = 0.7
    max_highlights: int = 5
    web_search_provider: str = "tavily"
    report_style: str = "standard"


@dataclass
class AgentConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    routing_enabled: bool = True
    fast_provider: str = ""
    fast_model: str = ""
    strong_provider: str = ""
    strong_model: str = ""
    haiku_model: str = "claude-haiku-4-5-20251001"
    summary_provider: str = "anthropic"
    ollama_base_url: str = "http://127.0.0.1:11434"
    max_tokens: int = 4096
    data_dir: str = "./data"

    # Cost budgets (USD per session)
    cost_budget_soft_usd: float = 0.50
    cost_budget_hard_usd: float = 2.00

    # Fallback providers (ordered list, tried after primary fails)
    fallback_providers: list[str] = field(default_factory=lambda: ["openai", "gemini"])
    # Fallback models per provider (overrides built-in defaults)
    fallback_models: dict[str, str] = field(default_factory=dict)

    # Routing mode: "heuristic" | "llm_judge" | "disabled"
    routing_mode: str = "heuristic"
    routing_classifier_provider: str = ""
    routing_classifier_model: str = ""

    # Hardcoded guards — NOT overridable by the agent at runtime
    MAX_ITERATIONS: int = 15
    MAX_TOKENS_SESSION: int = 100_000
    TIMEOUT_PER_TOOL_SECONDS: int = 30
    TIMEOUT_SESSION_SECONDS: int = 300
    MAX_TOOL_OUTPUT_CHARS: int = 10_000
    CONFIRMATION_TIMEOUT_SECONDS: int = 60


class _EnvSettings(BaseSettings):
    """Loads raw values from .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ANTHROPIC_API_KEY: str = Field(default="")
    OPENAI_API_KEY: str = Field(default="")
    GEMINI_API_KEY: str = Field(default="")
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_ALLOWED_USER_IDS: str = Field(default="")

    # Optional overrides
    EMERGENT_PROVIDER: str = Field(default="")
    EMERGENT_MODEL: str = Field(default="")
    EMERGENT_ROUTING_ENABLED: str = Field(default="")
    EMERGENT_FAST_PROVIDER: str = Field(default="")
    EMERGENT_FAST_MODEL: str = Field(default="")
    EMERGENT_STRONG_PROVIDER: str = Field(default="")
    EMERGENT_STRONG_MODEL: str = Field(default="")
    EMERGENT_HAIKU_MODEL: str = Field(default="")
    EMERGENT_SUMMARY_PROVIDER: str = Field(default="")
    EMERGENT_OLLAMA_BASE_URL: str = Field(default="")
    EMERGENT_DATA_DIR: str = Field(default="")
    EMERGENT_COST_BUDGET_SOFT_USD: str = Field(default="")
    EMERGENT_COST_BUDGET_HARD_USD: str = Field(default="")
    EMERGENT_FALLBACK_PROVIDERS: str = Field(default="")
    EMERGENT_ROUTING_MODE: str = Field(default="")
    EMERGENT_ROUTING_CLASSIFIER_PROVIDER: str = Field(default="")
    EMERGENT_ROUTING_CLASSIFIER_MODEL: str = Field(default="")
    TAVILY_API_KEY: str = Field(default="")
    GITHUB_TOKEN: str = Field(default="")


@dataclass
class EmergentSettings:
    """Assembled settings from .env + config.yaml."""

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    system_prompt: str = ""
    memory: dict[str, Any] = field(default_factory=dict)
    observability: dict[str, Any] = field(default_factory=dict)
    tools_config: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)


def _parse_user_ids(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_dict(raw: dict | Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if k and v}
    return {}


def _parse_list(env_raw: str, yaml_default: list[str]) -> list[str]:
    if env_raw.strip():
        return [x.strip() for x in env_raw.split(",") if x.strip()]
    if isinstance(yaml_default, list):
        return [str(x).strip() for x in yaml_default if str(x).strip()]
    return []


def _parse_bool(raw: str, default: bool) -> bool:
    text = raw.strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


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

    agent = AgentConfig(
        provider=env.EMERGENT_PROVIDER or agent_yaml.get("provider", "anthropic"),
        model=env.EMERGENT_MODEL or agent_yaml.get("model", "claude-sonnet-4-20250514"),
        routing_enabled=_parse_bool(
            env.EMERGENT_ROUTING_ENABLED,
            bool(agent_yaml.get("routing_enabled", True)),
        ),
        fast_provider=env.EMERGENT_FAST_PROVIDER or agent_yaml.get("fast_provider", ""),
        fast_model=env.EMERGENT_FAST_MODEL or agent_yaml.get("fast_model", ""),
        strong_provider=env.EMERGENT_STRONG_PROVIDER or agent_yaml.get("strong_provider", ""),
        strong_model=env.EMERGENT_STRONG_MODEL or agent_yaml.get("strong_model", ""),
        haiku_model=env.EMERGENT_HAIKU_MODEL
        or agent_yaml.get("haiku_model", "claude-haiku-4-5-20251001"),
        summary_provider=env.EMERGENT_SUMMARY_PROVIDER
        or agent_yaml.get("summary_provider", "anthropic"),
        ollama_base_url=env.EMERGENT_OLLAMA_BASE_URL
        or agent_yaml.get("ollama_base_url", "http://127.0.0.1:11434"),
        max_tokens=agent_yaml.get("max_tokens", 4096),
        data_dir=env.EMERGENT_DATA_DIR or agent_yaml.get("data_dir", "./data"),
        cost_budget_soft_usd=float(
            env.EMERGENT_COST_BUDGET_SOFT_USD or agent_yaml.get("cost_budget_soft_usd", 0.50)
        ),
        cost_budget_hard_usd=float(
            env.EMERGENT_COST_BUDGET_HARD_USD or agent_yaml.get("cost_budget_hard_usd", 2.00)
        ),
        fallback_providers=_parse_list(
            env.EMERGENT_FALLBACK_PROVIDERS or "",
            agent_yaml.get("fallback_providers", []),
        ),
        fallback_models=_parse_dict(agent_yaml.get("fallback_models", {})),
        routing_mode=env.EMERGENT_ROUTING_MODE
        or agent_yaml.get("routing_mode", "heuristic"),
        routing_classifier_provider=env.EMERGENT_ROUTING_CLASSIFIER_PROVIDER
        or agent_yaml.get("routing_classifier_provider", ""),
        routing_classifier_model=env.EMERGENT_ROUTING_CLASSIFIER_MODEL
        or agent_yaml.get("routing_classifier_model", ""),
    )

    settings = EmergentSettings(
        anthropic_api_key=env.ANTHROPIC_API_KEY,
        openai_api_key=env.OPENAI_API_KEY,
        gemini_api_key=env.GEMINI_API_KEY,
        telegram=telegram,
        agent=agent,
        research=ResearchConfig(
            enabled=bool(yaml_cfg.get("research", {}).get("enabled", True)),
            schedule=str(yaml_cfg.get("research", {}).get("schedule", "30 9 * * *")),
            max_findings_per_domain=int(
                yaml_cfg.get("research", {}).get("max_findings_per_domain", 10)
            ),
            highlight_threshold=float(yaml_cfg.get("research", {}).get("highlight_threshold", 0.7)),
            max_highlights=int(yaml_cfg.get("research", {}).get("max_highlights", 5)),
            web_search_provider=str(
                yaml_cfg.get("research", {}).get("web_search_provider", "tavily")
            ),
            report_style=str(yaml_cfg.get("research", {}).get("report_style", "standard")),
        ),
        system_prompt=yaml_cfg.get("system_prompt", ""),
        memory=yaml_cfg.get("memory", {}),
        observability=yaml_cfg.get("observability", {}),
        tools_config=yaml_cfg.get("tools", {}),
        voice=yaml_cfg.get("voice", {}),
    )

    # Make API key available as env var for the anthropic client
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    if settings.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
    if env.TAVILY_API_KEY:
        os.environ["TAVILY_API_KEY"] = env.TAVILY_API_KEY
    if env.GITHUB_TOKEN:
        os.environ["GITHUB_TOKEN"] = env.GITHUB_TOKEN

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

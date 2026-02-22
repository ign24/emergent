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
    haiku_model: str = "claude-haiku-4-5-20251001"
    summary_provider: str = "anthropic"
    ollama_base_url: str = "http://127.0.0.1:11434"
    max_tokens: int = 4096
    data_dir: str = "./data"

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
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_ALLOWED_USER_IDS: str = Field(default="")

    # Optional overrides
    EMERGENT_PROVIDER: str = Field(default="")
    EMERGENT_MODEL: str = Field(default="")
    EMERGENT_HAIKU_MODEL: str = Field(default="")
    EMERGENT_SUMMARY_PROVIDER: str = Field(default="")
    EMERGENT_OLLAMA_BASE_URL: str = Field(default="")
    EMERGENT_DATA_DIR: str = Field(default="")
    TAVILY_API_KEY: str = Field(default="")
    GITHUB_TOKEN: str = Field(default="")


@dataclass
class EmergentSettings:
    """Assembled settings from .env + config.yaml."""

    anthropic_api_key: str = ""
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
        haiku_model=env.EMERGENT_HAIKU_MODEL
        or agent_yaml.get("haiku_model", "claude-haiku-4-5-20251001"),
        summary_provider=env.EMERGENT_SUMMARY_PROVIDER
        or agent_yaml.get("summary_provider", "anthropic"),
        ollama_base_url=env.EMERGENT_OLLAMA_BASE_URL
        or agent_yaml.get("ollama_base_url", "http://127.0.0.1:11434"),
        max_tokens=agent_yaml.get("max_tokens", 4096),
        data_dir=env.EMERGENT_DATA_DIR or agent_yaml.get("data_dir", "./data"),
    )

    settings = EmergentSettings(
        anthropic_api_key=env.ANTHROPIC_API_KEY,
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

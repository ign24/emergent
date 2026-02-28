"""Domain and feed configuration for autonomous research runs."""

from __future__ import annotations

from typing import Any

RESEARCH_DOMAINS: dict[str, dict[str, Any]] = {
    "agent_architecture": {
        "keywords": ["agent", "orchestration", "tool", "react", "multi-agent"],
        "arxiv_queries": [
            "autonomous LLM agents",
            "tool use language models",
            "multi-agent orchestration",
        ],
        "arxiv_categories": ["cs.AI", "cs.CL", "cs.SE"],
        "github_queries": ["agent framework language:python stars:>50"],
        "hn_queries": ["AI agent", "autonomous agent", "tool use LLM"],
        "subreddits": ["MachineLearning", "LocalLLaMA"],
        "tavily_queries": ["LLM agent architecture patterns"],
    },
    "rag_memory": {
        "keywords": ["rag", "retrieval", "memory", "embedding", "vector"],
        "arxiv_queries": ["retrieval augmented generation", "semantic memory agents"],
        "arxiv_categories": ["cs.IR", "cs.CL"],
        "github_queries": ["RAG framework language:python stars:>30"],
        "hn_queries": ["RAG", "vector database", "embeddings"],
        "subreddits": ["MachineLearning"],
        "tavily_queries": ["RAG optimization techniques"],
    },
    "python_ecosystem": {
        "keywords": ["python", "asyncio", "typing", "performance", "release"],
        "github_queries": ["language:python stars:>100 topic:async"],
        "hn_queries": ["Python", "asyncio", "uv package manager"],
        "subreddits": ["Python"],
        "tavily_queries": ["Python asyncio best practices"],
        "releases_repos": ["astral-sh/uv", "pydantic/pydantic"],
    },
    "security_safety": {
        "keywords": ["security", "prompt", "injection", "guardrails", "safety"],
        "arxiv_queries": ["prompt injection defense", "LLM safety guardrails"],
        "arxiv_categories": ["cs.CR", "cs.AI"],
        "github_queries": ["LLM security language:python stars:>20"],
        "hn_queries": ["prompt injection", "AI safety", "LLM security"],
        "subreddits": ["netsec", "MachineLearning"],
        "tavily_queries": ["LLM agent security best practices"],
    },
    "devops_infra": {
        "keywords": ["deployment", "docker", "kubernetes", "monitoring", "observability"],
        "github_queries": ["agent deployment docker stars:>20"],
        "hn_queries": ["agent deployment", "LLM monitoring"],
        "subreddits": ["devops", "selfhosted"],
        "tavily_queries": ["autonomous agent deployment monitoring"],
    },
    "emergent_deps": {
        "keywords": ["release", "breaking", "migration", "deprecation", "security"],
        "releases_repos": [
            "anthropics/anthropic-sdk-python",
            "aiogram/aiogram",
            "chroma-core/chroma",
            "agronholm/apscheduler",
            "hynek/structlog",
            "astral-sh/uv",
            "encode/httpx",
        ],
    },
}

RSS_FEEDS: list[dict[str, str]] = [
    {"url": "https://www.anthropic.com/feed.xml", "domain": "agent_architecture"},
    {"url": "https://simonwillison.net/atom/everything/", "domain": "agent_architecture"},
    {"url": "https://openai.com/blog/rss.xml", "domain": "agent_architecture"},
    {"url": "https://blog.langchain.dev/rss/", "domain": "rag_memory"},
]

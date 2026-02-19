"""Agent-facing memory tools — search and store."""

from __future__ import annotations

import re
from typing import Any

import structlog

from emergent import SafetyViolationError
from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore

logger = structlog.get_logger(__name__)

# Secret patterns to block from being stored in memory
_SECRET_PATTERNS = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"sk-ant-api\d{2}-",
        r"sk-[a-zA-Z0-9]{40,}",
        r"(?i)password\s*[=:]\s*\S+",
        r"(?i)token\s*[=:]\s*\S{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"[A-Z0-9]{20}:[A-Za-z0-9/+]{40}",  # AWS-style key
        r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----",
    ]
]


def _check_for_secrets(value: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            logger.warning("secrets_detected_in_memory_store", value_preview=value[:20])
            raise SafetyViolationError(
                "SECRETS_DETECTED: value appears to contain sensitive credentials"
            )


def make_memory_search_handler(retriever: SemanticRetriever) -> Any:
    async def memory_search(tool_input: dict[str, Any]) -> str:
        query = str(tool_input.get("query", "")).strip()
        top_k = int(tool_input.get("top_k", 3))
        top_k = min(top_k, 5)

        if len(query) < 3:
            return "Error: query must be at least 3 characters"

        if len(query) > 200:
            return "Error: query exceeds 200 characters"

        try:
            results = await retriever.search(query, top_k=top_k)
        except Exception as e:
            logger.warning("memory_search_failed", error=str(e))
            return "No se encontraron memorias relevantes (ChromaDB no disponible)."

        if not results:
            return "No se encontraron memorias relevantes para esa búsqueda."

        lines = [f"Memorias relevantes para '{query}':"]
        for i, r in enumerate(results, 1):
            score = r["relevance_score"]
            lines.append(f"\n[{i}] (score: {score:.2f})\n{r['content']}")

        return "\n".join(lines)

    return memory_search


def make_memory_store_handler(store: MemoryStore) -> Any:
    async def memory_store(tool_input: dict[str, Any]) -> str:
        key = str(tool_input.get("key", "")).strip()
        value = str(tool_input.get("value", "")).strip()
        confidence = float(tool_input.get("confidence", 1.0))
        confidence = max(0.0, min(1.0, confidence))

        if not key:
            return "Error: key is required"

        if len(key) > 100:
            return "Error: key exceeds 100 characters"

        if not value:
            return "Error: value is required"

        if len(value) > 2000:
            return "Error: value exceeds 2000 characters"

        # Secret detection
        _check_for_secrets(value)

        try:
            await store.set_profile_key(key, value, confidence)
        except Exception as e:
            return f"Error: failed to store memory: {e}"

        logger.info("memory_stored", key=key, confidence=confidence)
        return f"Memoria guardada: '{key}' = '{value[:50]}...' (confidence: {confidence:.1f})"

    return memory_store


MEMORY_SEARCH_DEFINITION = {
    "name": "memory_search",
    "description": (
        "Search semantic memory for relevant past information. "
        "Returns top matching memories based on semantic similarity. "
        "Use this to recall previous conversations, user preferences, or stored facts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for (3-200 chars)",
                "minLength": 3,
                "maxLength": 200,
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (1-5). Default: 3.",
                "default": 3,
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["query"],
    },
}

MEMORY_STORE_DEFINITION = {
    "name": "memory_store",
    "description": (
        "Store a fact or preference in long-term memory with a descriptive key. "
        "Use this to remember user preferences, important information, or context. "
        "Secrets and credentials are blocked."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Descriptive key for the memory (e.g., 'user_preferred_editor')",
                "maxLength": 100,
            },
            "value": {
                "type": "string",
                "description": "The value to store",
                "maxLength": 2000,
            },
            "confidence": {
                "type": "number",
                "description": "Confidence level (0.0-1.0). Default: 1.0",
                "default": 1.0,
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "required": ["key", "value"],
    },
}

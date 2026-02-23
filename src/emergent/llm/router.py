"""LLM-as-judge prompt router.

Classifies incoming prompts into complexity tiers (FAST / DEFAULT / STRONG)
using a cheap LLM call, with LRU cache and fast-path heuristics.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from emergent.llm.client import LLMClient

logger = structlog.get_logger(__name__)

_CLASSIFIER_SYSTEM_PROMPT = """\
You are a prompt complexity classifier. Classify the user message into exactly \
one tier. Respond with a SINGLE word: FAST, DEFAULT, or STRONG.

Rules:
- FAST: trivial greetings, single-word queries, simple status checks, short factual lookups.
- DEFAULT: standard questions, moderate code tasks, explanations, summaries.
- STRONG: deep analysis, multi-step reasoning, architecture design, debugging, refactoring, \
research, comparisons across multiple dimensions.

Respond with only one word."""

_VALID_TIERS = frozenset({"FAST", "DEFAULT", "STRONG"})

# Cache config
_CACHE_MAX_SIZE = 256
_CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass(frozen=True)
class ClassificationResult:
    tier: str  # "FAST" | "DEFAULT" | "STRONG"
    cached: bool
    latency_ms: float
    cost_usd: float


class PromptRouter:
    """Classifies prompt complexity using a cheap LLM call with caching."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model
        self._cache: dict[str, tuple[str, float]] = {}  # key -> (tier, timestamp)

    async def classify(
        self,
        message: str,
        history_len: int = 0,
        tools_count: int = 0,
    ) -> ClassificationResult:
        """Classify a message into FAST / DEFAULT / STRONG tier."""
        t0 = time.monotonic()

        # Fast-path: very short messages → FAST without LLM call
        if len(message.strip()) <= 8:
            latency = (time.monotonic() - t0) * 1000
            return ClassificationResult(tier="FAST", cached=False, latency_ms=latency, cost_usd=0.0)

        # Check cache
        cache_key = self._cache_key(message)
        cached_entry = self._cache.get(cache_key)
        if cached_entry is not None:
            tier, ts = cached_entry
            if (time.monotonic() - ts) < _CACHE_TTL_SECONDS:
                latency = (time.monotonic() - t0) * 1000
                return ClassificationResult(tier=tier, cached=True, latency_ms=latency, cost_usd=0.0)
            # Expired
            del self._cache[cache_key]

        # LLM classify call
        try:
            from emergent.llm.pricing import calculate_cost

            response = await self._client.complete(
                model=self._model,
                system=_CLASSIFIER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message[:500]}],
                max_tokens=8,
            )
            tier = response.content[0].text.strip().upper() if response.content else "DEFAULT"
            if tier not in _VALID_TIERS:
                tier = "DEFAULT"

            cost = calculate_cost(
                self._model,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # Store in cache (evict oldest if full)
            if len(self._cache) >= _CACHE_MAX_SIZE:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[cache_key] = (tier, time.monotonic())

            latency = (time.monotonic() - t0) * 1000
            logger.debug(
                "router_classify",
                tier=tier,
                latency_ms=round(latency, 1),
                cost_usd=round(cost, 6),
                cached=False,
            )
            return ClassificationResult(tier=tier, cached=False, latency_ms=latency, cost_usd=cost)

        except Exception:
            logger.warning("router_classify_failed", exc_info=True)
            latency = (time.monotonic() - t0) * 1000
            return ClassificationResult(
                tier="DEFAULT", cached=False, latency_ms=latency, cost_usd=0.0
            )

    @staticmethod
    def _cache_key(message: str) -> str:
        """Hash first 200 chars for cache key."""
        snippet = message[:200]
        return hashlib.sha256(snippet.encode()).hexdigest()[:16]

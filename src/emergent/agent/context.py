"""Context window management — builds the message list for each LLM call."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore

logger = structlog.get_logger(__name__)

# Token budget per component (approximate; 1 token ≈ 4 chars)
_BUDGET = {
    "system_prompt": 800,  # fixed — never truncate
    "response_buffer": 4096,  # fixed — never truncate
    "user_profile": 300,  # drop first on overflow
    "semantic_memories": 600,  # reduce top_k: 3→1
    "session_summary": 400,  # drop if recent history exists
    "history": None,  # gets remaining budget
}

_TOTAL_CONTEXT_BUDGET = 20_000  # tokens
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total += _estimate_tokens(block["text"])
    return total


class ContextBuilder:
    """Builds the context for each LLM call with memory injection."""

    def __init__(
        self,
        store: MemoryStore,
        retriever: SemanticRetriever,
        context_budget_tokens: int = _TOTAL_CONTEXT_BUDGET,
        summarize_at_pct: float = 0.80,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._context_budget = context_budget_tokens
        self._summarize_at_pct = summarize_at_pct

    async def build_context(
        self,
        session_id: str,
        current_query: str,
        max_history_turns: int = 20,
    ) -> tuple[str | None, list[str] | None, str | None, list[dict[str, Any]]]:
        """
        Fetch all context components in parallel.

        Returns:
            (user_profile_text, semantic_memories, session_summary, history_messages)
        """
        # Fetch all in parallel with graceful degradation
        results = await asyncio.gather(
            self._store.get_profile_as_text(min_confidence=0.5),
            self._retriever.get_relevant_memories_as_text(current_query, top_k=3),
            self._store.get_session_summary(session_id),
            self._store.get_recent_history(session_id, max_turns=max_history_turns),
            return_exceptions=True,
        )

        profile_text: str | None = None
        memories: list[str] | None = None
        summary: str | None = None
        history: list[dict[str, Any]] = []

        if not isinstance(results[0], Exception):
            profile_text = results[0]  # type: ignore[assignment]
        else:
            logger.warning("profile_fetch_failed", error=str(results[0]))

        if not isinstance(results[1], Exception):
            memories = results[1]  # type: ignore[assignment]
        else:
            logger.warning("semantic_search_failed", error=str(results[1]))

        if not isinstance(results[2], Exception):
            summary = results[2]  # type: ignore[assignment]
        else:
            logger.warning("summary_fetch_failed", error=str(results[2]))

        if not isinstance(results[3], Exception):
            history = results[3]  # type: ignore[assignment]
        else:
            logger.warning("history_fetch_failed", error=str(results[3]))

        # Apply token budget constraints
        fixed_tokens = _BUDGET["system_prompt"] + _BUDGET["response_buffer"]
        available = self._context_budget - fixed_tokens

        profile_tokens = _estimate_tokens(profile_text) if profile_text else 0
        memories_tokens = sum(_estimate_tokens(m) for m in (memories or []))
        summary_tokens = _estimate_tokens(summary) if summary else 0
        history_tokens = _estimate_message_tokens(history)

        total_used = profile_tokens + memories_tokens + summary_tokens + history_tokens

        # Truncation cascade if over budget
        if total_used > available:
            # 1. Drop profile (lowest priority dynamic)
            if profile_tokens > 0 and total_used > available:
                logger.warning("context_budget_drop_profile", tokens_used=total_used)
                profile_text = None
                total_used -= profile_tokens

            # 2. Reduce memories to top_1
            if memories and len(memories) > 1 and total_used > available:
                logger.warning("context_budget_reduce_memories")
                memories = memories[:1]
                total_used = total_used - memories_tokens + _estimate_tokens(memories[0])

            # 3. Drop summary if we have recent history
            if summary and history and total_used > available:
                logger.warning("context_budget_drop_summary")
                summary = None
                total_used -= summary_tokens

            # 4. Truncate history
            while history and total_used > available and len(history) > 4:
                removed = history.pop(0)
                total_used -= _estimate_tokens(removed.get("content", ""))
                logger.warning("context_budget_truncate_history", remaining=len(history))

        logger.info(
            "context_built",
            session_id=session_id,
            has_profile=profile_text is not None,
            memory_count=len(memories) if memories else 0,
            has_summary=summary is not None,
            history_turns=len(history),
            estimated_tokens=total_used,
        )

        return profile_text, memories, summary, history

    def should_summarize(self, history: list[dict[str, Any]]) -> bool:
        """Check if history is long enough to warrant summarization."""
        history_tokens = _estimate_message_tokens(history)
        fixed_tokens = _BUDGET["system_prompt"] + _BUDGET["response_buffer"]
        available = self._context_budget - fixed_tokens
        return history_tokens > available * self._summarize_at_pct

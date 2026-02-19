"""Auto-summarization of long conversations using Haiku."""

from __future__ import annotations

from typing import Any

import anthropic
import structlog

logger = structlog.get_logger(__name__)

MIN_SUMMARY_CHARS = 50
MAX_SUMMARY_CHARS = 800
MAX_RETRIES = 2


async def summarize_conversation(
    client: anthropic.AsyncAnthropic,
    messages: list[dict[str, Any]],
    haiku_model: str = "claude-haiku-4-5-20251001",
) -> str | None:
    """
    Summarize a conversation using Haiku (cheap compression).

    Returns summary string or None if summarization fails.
    """
    if not messages:
        return None

    # Format messages for summarization
    conversation_text = "\n".join(
        f"[{m['role'].upper()}]: {m['content'][:500]}"
        for m in messages
        if isinstance(m.get("content"), str)
    )

    if len(conversation_text) < MIN_SUMMARY_CHARS:
        return None

    prompt = (
        "Resumí esta conversación en 2-4 oraciones. "
        "Enfocate en los temas principales, decisiones tomadas, "
        "y contexto importante para futuras interacciones. "
        "Sé conciso.\n\n"
        f"CONVERSACIÓN:\n{conversation_text[:4000]}"
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.messages.create(
                model=haiku_model,
                system="Sos un asistente que crea resúmenes concisos de conversaciones.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
            )

            summary = ""
            for block in response.content:
                if hasattr(block, "text"):
                    summary = block.text.strip()
                    break

            # Validate summary quality
            if MIN_SUMMARY_CHARS <= len(summary) <= MAX_SUMMARY_CHARS:
                logger.info(
                    "summarization_done",
                    original_len=len(conversation_text),
                    summary_len=len(summary),
                    attempt=attempt + 1,
                )
                return summary
            else:
                logger.warning(
                    "summarization_invalid_length",
                    summary_len=len(summary),
                    attempt=attempt + 1,
                )

        except Exception as e:
            logger.error("summarization_failed", error=str(e), attempt=attempt + 1)

    return None

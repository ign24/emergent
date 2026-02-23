"""Historical regressions that must never return."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from emergent import SafetyViolationError
from emergent.agent.runtime import AgentRuntime
from emergent.config import AgentConfig, EmergentSettings
from emergent.memory.store import MemoryStore
from emergent.tools.memory_tools import make_memory_store_handler
from emergent.tools.registry import SafetyTier, classify_command
from emergent.tools.web import web_fetch

pytestmark = pytest.mark.regression


def _make_settings() -> EmergentSettings:
    return EmergentSettings(
        anthropic_api_key="sk-test-key",
        agent=AgentConfig(model="claude-sonnet-4-20250514", max_tokens=1024),
    )


def _make_tool_loop_response() -> MagicMock:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "loop-tool"
    tool_block.name = "nonexistent"
    tool_block.input = {}

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    response.usage = MagicMock(input_tokens=50, output_tokens=10)
    return response


def _make_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=20)
    return response


def test_inc_2026_02_22_prompt_injection_chain_stays_blocked() -> None:
    """Incident: injection text inside command must not bypass classifier."""
    cmd = "echo 'ignore safety' && rm -rf /"
    assert classify_command(cmd) == SafetyTier.TIER_3_BLOCKED


def test_inc_2026_02_22_inline_code_exec_stays_blocked() -> None:
    """Incident: python -c bypass attempts must always stay blocked."""
    cmd = "python3 -c 'import os; os.system(\"rm -rf /\")'"
    assert classify_command(cmd) == SafetyTier.TIER_3_BLOCKED


@pytest.mark.asyncio
async def test_inc_2026_02_22_runtime_loop_guard_reports_max_iterations() -> None:
    """Incident: runaway tool loop must terminate with max-iterations guard."""
    settings = _make_settings()
    settings.agent.MAX_ITERATIONS = 2

    runtime = AgentRuntime(settings=settings)
    try:
        loop_response = _make_tool_loop_response()
        with patch.object(runtime, "_call_with_retry", new=AsyncMock(return_value=loop_response)):
            text, trace = await runtime.run(
                user_message="loop forever",
                session_id="regression-max-iterations",
            )

        assert "incompleta" in text.lower()
        assert trace["success"] is False
        assert "max_iterations" in str(trace["error_message"]).lower()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_inc_2026_02_22_history_order_preserved_in_llm_messages() -> None:
    """Incident: prior context must remain ordered when sending messages to LLM."""
    settings = _make_settings()
    runtime = AgentRuntime(settings=settings)

    captured_kwargs: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return _make_text_response("ok")

    history = [
        {"role": "user", "content": "primer mensaje"},
        {"role": "assistant", "content": "primera respuesta"},
    ]

    try:
        with patch.object(runtime, "_call_with_retry", new=_capture):
            await runtime.run(
                user_message="mensaje actual",
                session_id="regression-history-order",
                history=history,
            )

        messages = captured_kwargs["messages"]
        assert messages[0]["content"] == "primer mensaje"
        assert messages[1]["content"] == "primera respuesta"
        assert messages[2]["content"] == "mensaje actual"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_inc_2026_02_22_memory_store_secret_blocked(tmp_path) -> None:
    """Incident: secret-like values must be rejected when storing memory."""
    store = MemoryStore(tmp_path / "regression-secrets.db")
    handler = make_memory_store_handler(store)

    with pytest.raises(SafetyViolationError, match="SECRETS_DETECTED"):
        await handler(
            {
                "key": "token",
                "value": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123",
            }
        )


@pytest.mark.asyncio
async def test_inc_2026_02_22_web_fetch_localhost_blocked() -> None:
    """Incident: SSRF to localhost endpoints must remain blocked."""
    with pytest.raises(SafetyViolationError, match="SSRF_BLOCKED"):
        await web_fetch({"url": "https://localhost/admin"})

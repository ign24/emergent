"""E2E tests for the AgentRuntime loop — Anthropic API is mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from emergent.agent.runtime import AgentRuntime
from emergent.config import AgentConfig, EmergentSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> EmergentSettings:
    return EmergentSettings(
        anthropic_api_key="sk-test-key",
        agent=AgentConfig(model="claude-sonnet-4-20250514", max_tokens=1024),
    )


def _make_text_response(text: str) -> MagicMock:
    """Simulate a Claude response with stop_reason='end_turn'."""
    block = MagicMock()
    block.type = "text"
    block.text = text

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=20)
    return response


def _make_tool_then_text_response(
    tool_name: str, tool_input: dict[str, Any], tool_id: str, final_text: str
) -> tuple[MagicMock, MagicMock]:
    """Two responses: first tool_use, then end_turn after tool result."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = tool_id
    tool_block.name = tool_name
    tool_block.input = tool_input

    tool_response = MagicMock()
    tool_response.stop_reason = "tool_use"
    tool_response.content = [tool_block]
    tool_response.usage = MagicMock(input_tokens=200, output_tokens=30)

    text_response = _make_text_response(final_text)
    return tool_response, text_response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_end_turn():
    """Agent returns text response directly without tools."""
    settings = _make_settings()
    runtime = AgentRuntime(settings=settings)

    with patch.object(runtime, "_call_with_retry", new=AsyncMock(return_value=_make_text_response("Hola!"))):
        text, trace = await runtime.run(
            user_message="Hola",
            session_id="test-session",
        )

    assert text == "Hola!"
    assert trace["success"] is True
    assert trace["iterations"] == 1
    assert trace["tools_called"] == []


@pytest.mark.asyncio
async def test_tool_use_tier1_auto():
    """Agent calls a TIER_1 tool, gets result, returns final text."""
    from emergent.tools.registry import SafetyTier, ToolDefinition, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echoes input",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
            handler=AsyncMock(return_value="echoed: hello"),
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )

    settings = _make_settings()
    runtime = AgentRuntime(settings=settings, registry=registry)

    tool_resp, text_resp = _make_tool_then_text_response(
        tool_name="echo",
        tool_input={"msg": "hello"},
        tool_id="tool_abc123",
        final_text="El echo fue: echoed: hello",
    )

    with patch.object(runtime, "_call_with_retry", new=AsyncMock(side_effect=[tool_resp, text_resp])):
        text, trace = await runtime.run(
            user_message="Hacé un echo de hello",
            session_id="test-session",
        )

    assert text == "El echo fue: echoed: hello"
    assert trace["success"] is True
    assert trace["iterations"] == 2
    assert "echo" in trace["tools_called"]


@pytest.mark.asyncio
async def test_tool_use_tier3_blocked():
    """TIER_3 tools are blocked and agent gets error result."""
    from emergent.tools.registry import SafetyTier, ToolDefinition, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="shell_execute",
            description="Shell",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            handler=AsyncMock(return_value="should not run"),
            safety_tier=SafetyTier.TIER_3_BLOCKED,
        )
    )

    settings = _make_settings()
    runtime = AgentRuntime(settings=settings, registry=registry)

    # Override classify so shell_execute is always TIER_3
    from emergent.tools.registry import SafetyTier as ST
    registry.classify = MagicMock(return_value=ST.TIER_3_BLOCKED)

    tool_resp, text_resp = _make_tool_then_text_response(
        tool_name="shell_execute",
        tool_input={"command": "rm -rf /"},
        tool_id="tool_danger",
        final_text="No puedo hacer eso.",
    )

    with patch.object(runtime, "_call_with_retry", new=AsyncMock(side_effect=[tool_resp, text_resp])):
        text, trace = await runtime.run(
            user_message="Borrá todo",
            session_id="test-session",
        )

    assert text == "No puedo hacer eso."
    assert "shell_execute" in trace["tools_called"]


@pytest.mark.asyncio
async def test_max_iterations_raises():
    """Agent raises MaxIterationsError after hitting max_iterations."""
    from emergent import MaxIterationsError

    settings = _make_settings()
    settings.agent.MAX_ITERATIONS = 2

    runtime = AgentRuntime(settings=settings)

    # Always return tool_use (infinite loop simulation)
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "t1"
    tool_block.name = "nonexistent"
    tool_block.input = {}

    loop_response = MagicMock()
    loop_response.stop_reason = "tool_use"
    loop_response.content = [tool_block]
    loop_response.usage = MagicMock(input_tokens=50, output_tokens=10)

    with patch.object(runtime, "_call_with_retry", new=AsyncMock(return_value=loop_response)):
        text, trace = await runtime.run(
            user_message="Loop forever",
            session_id="test-session",
        )

    assert trace["success"] is False
    assert "max_iterations" in trace["error_message"].lower()


@pytest.mark.asyncio
async def test_history_is_passed_to_llm():
    """Existing history is included in the messages sent to Claude."""
    settings = _make_settings()
    runtime = AgentRuntime(settings=settings)

    history = [
        {"role": "user", "content": "Mensaje anterior"},
        {"role": "assistant", "content": "Respuesta anterior"},
    ]

    captured_kwargs: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return _make_text_response("ok")

    with patch.object(runtime, "_call_with_retry", new=_capture):
        await runtime.run(
            user_message="Nuevo mensaje",
            session_id="test-session",
            history=history,
        )

    messages = captured_kwargs["messages"]
    # Runtime appends assistant turn after capture, so list grows to 4 after run().
    # We assert the structure at call time: 2 history + 1 new user message at index 2.
    assert messages[0]["content"] == "Mensaje anterior"
    assert messages[1]["content"] == "Respuesta anterior"
    assert messages[2]["content"] == "Nuevo mensaje"


@pytest.mark.asyncio
async def test_retry_on_rate_limit():
    """API call is retried on RateLimitError before succeeding."""
    import anthropic

    settings = _make_settings()
    runtime = AgentRuntime(settings=settings)

    call_count = 0

    async def _flaky(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body={},
            )
        return _make_text_response("Finalmente!")

    with patch.object(runtime._client.messages, "create", new=_flaky):
        text, trace = await runtime.run(
            user_message="Intentá",
            session_id="test-session",
        )

    assert text == "Finalmente!"
    assert call_count == 3
    assert trace["success"] is True


@pytest.mark.asyncio
async def test_api_error_returns_graceful_message():
    """Unrecoverable API error returns user-friendly message."""
    import anthropic

    settings = _make_settings()
    runtime = AgentRuntime(settings=settings)

    async def _fail(**kwargs: Any) -> Any:
        raise anthropic.APIError(
            message="server exploded",
            request=MagicMock(),
            body={},
        )

    with patch.object(runtime, "_call_with_retry", new=_fail):
        text, trace = await runtime.run(
            user_message="Algo",
            session_id="test-session",
        )

    assert trace["success"] is False
    assert "error" in text.lower() or "Claude" in text

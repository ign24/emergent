"""AgentRuntime — Core agentic loop.

Implements ReAct pattern using Claude's native tool_use.

Loop:
    1. Build context (system prompt + memory + history + tool defs)
    2. Call Claude API
    3. If stop_reason == "tool_use": classify safety → execute/confirm/block → append result → goto 2
    4. If stop_reason == "end_turn": return text response
    5. Post-loop: persist conversation, emit traces, trigger summarization if needed

Guards (hardcoded, NOT configurable by the agent):
    - max_iterations: 15
    - max_tokens_session: 100_000
    - timeout_per_tool: 30 seconds
    - timeout_session: 300 seconds (5 min)
    - max_tool_output_chars: 10_000
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anthropic
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from emergent import (
    ContextOverflowError,
    MaxIterationsError,
    SafetyViolationError,
)
from emergent.agent.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt
from emergent.config import EmergentSettings

if TYPE_CHECKING:
    from emergent.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Type alias for confirmation callback
ConfirmCallback = Callable[[str, str], Awaitable[bool]]

# Cost per million tokens (verify with context7 / anthropic pricing page)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-20250514": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
}


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input_per_mtok": 3.00, "output_per_mtok": 15.00})
    return (
        input_tokens * pricing["input_per_mtok"] + output_tokens * pricing["output_per_mtok"]
    ) / 1_000_000


class AgentRuntime:
    """Core agentic loop using Claude's native tool_use."""

    def __init__(
        self,
        settings: EmergentSettings,
        registry: ToolRegistry | None = None,
        confirm_callback: ConfirmCallback | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._confirm_callback = confirm_callback
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        # Hardcoded guards
        self._MAX_ITERATIONS = settings.agent.MAX_ITERATIONS
        self._MAX_TOKENS_SESSION = settings.agent.MAX_TOKENS_SESSION
        self._TIMEOUT_PER_TOOL = settings.agent.TIMEOUT_PER_TOOL_SECONDS
        self._TIMEOUT_SESSION = settings.agent.TIMEOUT_SESSION_SECONDS
        self._MAX_OUTPUT_CHARS = settings.agent.MAX_TOOL_OUTPUT_CHARS

    async def run(
        self,
        user_message: str,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        user_profile: str | None = None,
        semantic_memories: list[str] | None = None,
        confirm_callback: ConfirmCallback | None = None,
        session_summary: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        Run the agentic loop for a single user turn.

        Returns:
            (response_text, trace_data) where trace_data contains metrics.
        """
        trace_id = str(uuid.uuid4())
        session_start = time.monotonic()
        total_input_tokens = 0
        total_output_tokens = 0
        iterations = 0
        tools_called: list[str] = []
        error_message: str | None = None

        log = logger.bind(trace_id=trace_id, session_id=session_id)
        log.info("agent_run_start", user_message_len=len(user_message))

        # Build system prompt with memory context
        built_system = build_system_prompt(
            base_prompt=system_prompt or self._settings.system_prompt or DEFAULT_SYSTEM_PROMPT,
            user_profile=user_profile,
            semantic_memories=semantic_memories,
            session_summary=session_summary,
        )

        # Build messages list
        messages: list[dict[str, Any]] = list(history or [])
        messages.append({"role": "user", "content": user_message})

        # Get tool definitions if registry is available
        tool_defs = self._registry.get_tool_definitions() if self._registry else []

        response_text = ""
        try:
            async with asyncio.timeout(self._TIMEOUT_SESSION):
                while iterations < self._MAX_ITERATIONS:
                    iterations += 1

                    # Check token budget
                    if total_input_tokens + total_output_tokens >= self._MAX_TOKENS_SESSION:
                        raise ContextOverflowError(
                            f"Session token limit ({self._MAX_TOKENS_SESSION}) exceeded"
                        )

                    # LLM call
                    call_start = time.monotonic()
                    log.info(
                        "llm_call",
                        iteration=iterations,
                        messages_count=len(messages),
                        tools_count=len(tool_defs),
                    )

                    kwargs: dict[str, Any] = {
                        "model": self._settings.agent.model,
                        "system": built_system,
                        "messages": messages,
                        "max_tokens": self._settings.agent.max_tokens,
                    }
                    if tool_defs:
                        kwargs["tools"] = tool_defs

                    response = await self._call_with_retry(**kwargs)

                    call_duration_ms = (time.monotonic() - call_start) * 1000
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    cost_usd = _calculate_cost(
                        self._settings.agent.model,
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    )

                    log.info(
                        "llm_call_done",
                        iteration=iterations,
                        stop_reason=response.stop_reason,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cost_usd=round(cost_usd, 6),
                        duration_ms=round(call_duration_ms),
                    )

                    # Append assistant response to history
                    messages.append({"role": "assistant", "content": response.content})

                    if response.stop_reason == "end_turn":
                        # Extract text from response
                        for block in response.content:
                            if hasattr(block, "text"):
                                response_text = block.text
                                break
                        break

                    elif response.stop_reason == "tool_use":
                        # Process tool calls
                        tool_results = await self._handle_tool_calls(
                            response.content,
                            trace_id=trace_id,
                            tools_called=tools_called,
                            log=log,
                            confirm_callback=confirm_callback or self._confirm_callback,
                        )
                        messages.append({"role": "user", "content": tool_results})

                    else:
                        # Unexpected stop reason
                        log.warning("unexpected_stop_reason", stop_reason=response.stop_reason)
                        for block in response.content:
                            if hasattr(block, "text"):
                                response_text = block.text
                                break
                        break

                else:
                    raise MaxIterationsError(
                        f"Agent loop hit max_iterations={self._MAX_ITERATIONS}"
                    )

        except TimeoutError:
            error_message = "Session timeout"
            response_text = "Lo siento, la operación tardó demasiado y fue cancelada."
            log.error("session_timeout", elapsed_s=round(time.monotonic() - session_start))
        except MaxIterationsError as e:
            error_message = str(e)
            response_text = "Alcancé el límite de iteraciones. La tarea puede estar incompleta."
            log.error("max_iterations_hit", iterations=iterations)
        except ContextOverflowError as e:
            error_message = str(e)
            response_text = "El contexto de la sesión es demasiado largo. Por favor, empezá una nueva conversación."
            log.error("context_overflow")
        except SafetyViolationError as e:
            error_message = str(e)
            response_text = f"Operación bloqueada por seguridad: {e}"
            log.warning("safety_violation_in_run", error=str(e))
        except anthropic.APIError as e:
            error_message = f"API error: {e}"
            response_text = "Hubo un error al comunicarme con Claude. Por favor, intentá de nuevo."
            log.error("anthropic_api_error", error=str(e))

        session_duration_ms = (time.monotonic() - session_start) * 1000
        total_cost = _calculate_cost(
            self._settings.agent.model,
            total_input_tokens,
            total_output_tokens,
        )

        trace_data: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "iterations": iterations,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost_usd": round(total_cost, 6),
            "duration_ms": round(session_duration_ms),
            "tools_called": tools_called,
            "success": error_message is None,
            "error_message": error_message,
        }

        log.info(
            "agent_run_done",
            **{k: v for k, v in trace_data.items() if k not in ("trace_id", "session_id")},
        )

        return response_text, trace_data

    @retry(
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APITimeoutError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_with_retry(self, **kwargs: Any) -> Any:
        """Call Claude API with automatic retry on transient errors."""
        return await self._client.messages.create(**kwargs)

    async def _handle_tool_calls(
        self,
        content_blocks: list[Any],
        trace_id: str,
        tools_called: list[str],
        log: Any,
        confirm_callback: ConfirmCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Process all tool_use blocks from a response."""
        tool_use_blocks = [b for b in content_blocks if b.type == "tool_use"]
        tool_results: list[dict[str, Any]] = []

        if not self._registry:
            # No registry: return error for all tools
            for block in tool_use_blocks:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Error: no tool registry configured",
                        "is_error": True,
                    }
                )
            return tool_results

        # Separate TIER_1 (auto) from TIER_2+ (confirm/block)
        from emergent.tools.registry import SafetyTier

        tier1_blocks = []
        other_blocks = []

        for block in tool_use_blocks:
            tier = self._registry.classify(block.name, block.input)
            if tier == SafetyTier.TIER_1_AUTO:
                tier1_blocks.append(block)
            else:
                other_blocks.append((block, tier))

        # Execute TIER_1 tools in parallel
        if tier1_blocks:
            results = await asyncio.gather(
                *[self._execute_tool(b, trace_id, log) for b in tier1_blocks],
                return_exceptions=True,
            )
            for block, result in zip(tier1_blocks, results):
                tools_called.append(block.name)
                if isinstance(result, Exception):
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {result}",
                            "is_error": True,
                        }
                    )
                else:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }
                    )

        # Handle TIER_2 and TIER_3 sequentially
        for block, tier in other_blocks:
            tools_called.append(block.name)
            if tier == SafetyTier.TIER_3_BLOCKED:
                log.warning(
                    "tier3_blocked",
                    tool_name=block.name,
                    command=str(block.input.get("command", ""))[:50],
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "BLOQUEADO: Este comando está bloqueado por seguridad.",
                        "is_error": True,
                    }
                )
            elif tier == SafetyTier.TIER_2_CONFIRM:
                result = await self._handle_tier2(block, trace_id, log, confirm_callback)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": result.startswith("Error:") or result.startswith("CANCELADO"),
                    }
                )

        return tool_results

    async def _execute_tool(
        self,
        block: Any,
        trace_id: str,
        log: Any,
    ) -> str:
        """Execute a tool with timeout, truncating output."""
        tool_start = time.monotonic()
        log.info("tool_exec_start", tool_name=block.name, tool_id=block.id)

        try:
            result = await asyncio.wait_for(
                self._registry.execute(block.name, block.input),  # type: ignore[union-attr]
                timeout=self._TIMEOUT_PER_TOOL,
            )
        except TimeoutError:
            log.error("tool_timeout", tool_name=block.name, timeout_s=self._TIMEOUT_PER_TOOL)
            return f"Error: tool '{block.name}' timed out after {self._TIMEOUT_PER_TOOL}s"
        except Exception as e:
            log.error("tool_exec_error", tool_name=block.name, error=str(e))
            return f"Error: {e}"

        duration_ms = (time.monotonic() - tool_start) * 1000

        # Truncate output
        result_str = str(result)
        truncated = False
        if len(result_str) > self._MAX_OUTPUT_CHARS:
            result_str = result_str[: self._MAX_OUTPUT_CHARS] + "\n[... output truncated]"
            truncated = True

        log.info(
            "tool_exec_done",
            tool_name=block.name,
            duration_ms=round(duration_ms),
            output_len=len(result_str),
            truncated=truncated,
        )
        return result_str

    async def _handle_tier2(
        self, block: Any, trace_id: str, log: Any, confirm_callback: ConfirmCallback | None = None
    ) -> str:
        """Handle TIER_2 tool requiring user confirmation."""
        command_preview = str(block.input.get("command", block.name))[:80]
        log.info("tier2_confirmation_requested", tool_name=block.name, preview=command_preview)

        cb = confirm_callback or self._confirm_callback
        if cb is None:
            # No confirmation callback — auto-deny in headless mode
            log.warning("tier2_no_callback_auto_deny", tool_name=block.name)
            return "CANCELADO: operación requiere confirmación del usuario (modo headless)."

        try:
            confirmed = await asyncio.wait_for(
                cb(block.name, command_preview),
                timeout=self._settings.agent.CONFIRMATION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning("tier2_confirmation_timeout", tool_name=block.name)
            return "CANCELADO: tiempo de confirmación agotado."

        if not confirmed:
            log.info("tier2_denied", tool_name=block.name)
            return "CANCELADO: el usuario rechazó la operación."

        log.info("tier2_approved", tool_name=block.name)
        return await self._execute_tool(block, trace_id, log)

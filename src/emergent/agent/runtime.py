"""AgentRuntime — Core agentic loop.

Implements ReAct pattern with provider-agnostic tool use.

Loop:
    1. Build context (system prompt + memory + history + tool defs)
    2. Call configured LLM provider API
    3. If stop_reason == "tool_use": classify safety and execute/confirm/block tools
       then append results and return to step 2
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
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from emergent import (
    ContextOverflowError,
    EmergentError,
    LLMProviderError,
    LLMRetryableError,
    MaxIterationsError,
    SafetyViolationError,
)
from emergent.agent.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt
from emergent.config import EmergentSettings
from emergent.llm.factory import create_llm_client
from emergent.llm.pricing import calculate_cost

if TYPE_CHECKING:
    from emergent.llm.router import PromptRouter
    from emergent.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Type alias for confirmation callback
ConfirmCallback = Callable[[str, str], Awaitable[bool]]
TextDeltaCallback = Callable[[str], Awaitable[None]]


class CostBudgetExceededError(EmergentError):
    """Raised when the session hard cost budget is exceeded."""


@dataclass(frozen=True)
class _RoutingDecision:
    provider: str
    model: str
    tier: str
    reason: str


# Default fallback models per provider (used when falling back to a provider
# without an explicit model configured).
_DEFAULT_FALLBACK_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}


class AgentRuntime:
    """Core agentic loop using provider-agnostic tool use."""

    def __init__(
        self,
        settings: EmergentSettings,
        registry: ToolRegistry | None = None,
        confirm_callback: ConfirmCallback | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._confirm_callback = confirm_callback
        self._clients: dict[str, Any] = {}
        self._client = self._get_or_create_client(settings.agent.provider)

        # Hardcoded guards
        self._MAX_ITERATIONS = settings.agent.MAX_ITERATIONS
        self._MAX_TOKENS_SESSION = settings.agent.MAX_TOKENS_SESSION
        self._TIMEOUT_PER_TOOL = settings.agent.TIMEOUT_PER_TOOL_SECONDS
        self._TIMEOUT_SESSION = settings.agent.TIMEOUT_SESSION_SECONDS
        self._MAX_OUTPUT_CHARS = settings.agent.MAX_TOOL_OUTPUT_CHARS

        # Session cost accumulator
        self._session_cost_usd: float = 0.0

        # LLM-as-judge router (Phase 3)
        self._router: PromptRouter | None = None
        if settings.agent.routing_mode == "llm_judge":
            self._init_router()

    def _init_router(self) -> None:
        """Initialize LLM-as-judge router with configured classifier."""
        from emergent.llm.router import PromptRouter

        agent_cfg = self._settings.agent
        classifier_provider = agent_cfg.routing_classifier_provider or agent_cfg.provider
        classifier_model = agent_cfg.routing_classifier_model or agent_cfg.haiku_model
        client = self._get_or_create_client(classifier_provider)
        self._router = PromptRouter(client=client, model=classifier_model)

    async def close(self) -> None:
        """Close runtime resources (HTTP client connections)."""
        for client in self._clients.values():
            await client.close()

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
        on_text_delta: TextDeltaCallback | None = None,
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
        selected_provider = self._settings.agent.provider
        selected_model = self._settings.agent.model
        selected_tier = "default"
        routing_reason = "fixed"
        fallback_used = False
        original_provider: str | None = None
        classifier_cost_usd = 0.0
        classifier_latency_ms = 0.0

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

        # Model routing: LLM-as-judge or heuristic
        decision = await self._route_model_for_turn(
            user_message=user_message,
            history=history or [],
            tools_count=len(tool_defs),
        )
        selected_provider = decision.provider
        selected_model = decision.model
        selected_tier = decision.tier
        routing_reason = decision.reason

        # Track classifier cost if LLM-as-judge was used
        if routing_reason.startswith("llm_judge"):
            classifier_cost_usd = getattr(decision, "_classifier_cost_usd", 0.0)
            classifier_latency_ms = getattr(decision, "_classifier_latency_ms", 0.0)

        llm_client = self._get_or_create_client(selected_provider)

        response_text = ""
        try:
            # Check hard budget before starting
            self._check_hard_budget(log)

            async with asyncio.timeout(self._TIMEOUT_SESSION):
                while iterations < self._MAX_ITERATIONS:
                    iterations += 1

                    # Check token budget
                    if total_input_tokens + total_output_tokens >= self._MAX_TOKENS_SESSION:
                        raise ContextOverflowError(
                            f"Session token limit ({self._MAX_TOKENS_SESSION}) exceeded"
                        )

                    # Check hard budget before each LLM call
                    self._check_hard_budget(log)

                    # LLM call
                    call_start = time.monotonic()
                    log.info(
                        "llm_call",
                        iteration=iterations,
                        provider=selected_provider,
                        model=selected_model,
                        model_tier=selected_tier,
                        routing_reason=routing_reason,
                        messages_count=len(messages),
                        tools_count=len(tool_defs),
                    )

                    response, actual_provider, actual_model = (
                        await self._call_with_retry_and_fallback(
                            client=llm_client,
                            provider=selected_provider,
                            model=selected_model,
                            system=built_system,
                            messages=messages,
                            max_tokens=self._settings.agent.max_tokens,
                            tools=tool_defs or None,
                            stream=on_text_delta is not None,
                            on_text_delta=on_text_delta,
                        )
                    )

                    # Track if fallback was used
                    if actual_provider != selected_provider:
                        if not fallback_used:
                            original_provider = selected_provider
                        fallback_used = True
                        selected_provider = actual_provider
                        selected_model = actual_model
                        llm_client = self._get_or_create_client(actual_provider)

                    call_duration_ms = (time.monotonic() - call_start) * 1000
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    cost_usd = calculate_cost(
                        actual_model,
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    )
                    self._session_cost_usd += cost_usd

                    log.info(
                        "llm_call_done",
                        iteration=iterations,
                        stop_reason=response.stop_reason,
                        provider=actual_provider,
                        model=actual_model,
                        model_tier=selected_tier,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cost_usd=round(cost_usd, 6),
                        session_cost_usd=round(self._session_cost_usd, 6),
                        duration_ms=round(call_duration_ms),
                    )

                    # Soft budget warning
                    self._check_soft_budget(log)

                    # Append assistant response to history
                    messages.append({"role": "assistant", "content": response.content_as_dicts()})

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
            response_text = (
                "El contexto de la sesión es demasiado largo. "
                "Por favor, empezá una nueva conversación."
            )
            log.error("context_overflow")
        except CostBudgetExceededError as e:
            error_message = str(e)
            response_text = (
                "Se alcanzó el límite de costo de la sesión. "
                "Por favor, empezá una nueva conversación."
            )
            log.error("cost_budget_exceeded", session_cost_usd=round(self._session_cost_usd, 6))
        except SafetyViolationError as e:
            error_message = str(e)
            response_text = f"Operación bloqueada por seguridad: {e}"
            log.warning("safety_violation_in_run", error=str(e))
        except LLMProviderError as e:
            error_message = f"API error: {e}"
            response_text = (
                "Hubo un error al comunicarme con el proveedor LLM. Por favor, intentá de nuevo."
            )
            log.error("llm_api_error", error=str(e), provider=selected_provider)

        session_duration_ms = (time.monotonic() - session_start) * 1000
        total_cost = calculate_cost(
            selected_model,
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
            "session_cost_usd": round(self._session_cost_usd, 6),
            "provider": selected_provider,
            "model": selected_model,
            "model_tier": selected_tier,
            "routing_reason": routing_reason,
            "fallback_used": fallback_used,
            "original_provider": original_provider,
            "classifier_cost_usd": round(classifier_cost_usd, 6),
            "classifier_latency_ms": round(classifier_latency_ms, 1),
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

    def _check_hard_budget(self, log: Any) -> None:
        """Raise CostBudgetExceededError if hard budget is reached."""
        hard = self._settings.agent.cost_budget_hard_usd
        if hard > 0 and self._session_cost_usd >= hard:
            raise CostBudgetExceededError(
                f"Session cost ${self._session_cost_usd:.4f} >= hard budget ${hard:.2f}"
            )

    def _check_soft_budget(self, log: Any) -> None:
        """Log warning when soft budget is reached."""
        soft = self._settings.agent.cost_budget_soft_usd
        if soft > 0 and self._session_cost_usd >= soft:
            log.warning(
                "cost_budget_soft_warning",
                session_cost_usd=round(self._session_cost_usd, 6),
                soft_budget_usd=soft,
            )

    async def _call_with_retry_and_fallback(
        self,
        *,
        client: Any,
        provider: str,
        model: str,
        stream: bool = False,
        on_text_delta: TextDeltaCallback | None = None,
        **kwargs: Any,
    ) -> tuple[Any, str, str]:
        """Call LLM API with retry on transient errors + fallback on fatal errors.

        Returns (response, actual_provider, actual_model).
        """
        try:
            response = await self._call_with_retry(
                client=client, stream=stream, on_text_delta=on_text_delta, model=model, **kwargs
            )
            return response, provider, model
        except (LLMProviderError, LLMRetryableError):
            # Try fallback providers
            fallback_providers = self._settings.agent.fallback_providers
            if not fallback_providers:
                raise

            log = logger.bind(original_provider=provider)
            for fb_provider in fallback_providers:
                if fb_provider.strip().lower() == provider.strip().lower():
                    continue
                fb_model = self._resolve_fallback_model(fb_provider)
                log.warning(
                    "fallback_attempt",
                    fallback_provider=fb_provider,
                    fallback_model=fb_model,
                )
                try:
                    fb_client = self._get_or_create_client(fb_provider)
                    response = await self._call_with_retry(
                        client=fb_client,
                        stream=stream,
                        on_text_delta=on_text_delta,
                        model=fb_model,
                        **kwargs,
                    )
                    log.info(
                        "fallback_success",
                        fallback_provider=fb_provider,
                        fallback_model=fb_model,
                    )
                    return response, fb_provider, fb_model
                except (LLMProviderError, LLMRetryableError):
                    log.warning("fallback_failed", fallback_provider=fb_provider)
                    continue

            # All fallbacks exhausted — re-raise original
            raise

    def _resolve_fallback_model(self, provider: str) -> str:
        """Map a fallback provider to its default model.

        Priority: config fallback_models > built-in defaults.
        """
        key = provider.strip().lower()
        configured = self._settings.agent.fallback_models.get(key)
        if configured:
            return configured
        return _DEFAULT_FALLBACK_MODELS.get(key, "default")

    async def _call_with_retry(
        self,
        *,
        client: Any,
        stream: bool = False,
        on_text_delta: TextDeltaCallback | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call LLM API with retry on transient provider errors."""
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                if stream and on_text_delta is not None and hasattr(client, "stream_complete"):
                    return await client.stream_complete(on_text_delta=on_text_delta, **kwargs)
                return await client.complete(**kwargs)
            except LLMRetryableError:
                if attempt >= attempts:
                    raise
                delay_seconds = min(2 ** (attempt - 1), 30)
                await asyncio.sleep(delay_seconds)

    def _get_or_create_client(self, provider: str) -> Any:
        key = provider.strip().lower()
        if key not in self._clients:
            self._clients[key] = create_llm_client(self._settings, key)
        return self._clients[key]

    async def _route_model_for_turn(
        self,
        *,
        user_message: str,
        history: list[dict[str, Any]],
        tools_count: int,
    ) -> _RoutingDecision:
        """Route using LLM-as-judge or heuristic based on config."""
        agent_cfg = self._settings.agent

        # Disabled routing
        if agent_cfg.routing_mode == "disabled" or not agent_cfg.routing_enabled:
            return _RoutingDecision(
                provider=agent_cfg.provider,
                model=agent_cfg.model,
                tier="default",
                reason="routing_disabled",
            )

        # LLM-as-judge routing
        if self._router is not None:
            result = await self._router.classify(
                message=user_message,
                history_len=len(history),
                tools_count=tools_count,
            )
            decision = self._tier_to_routing_decision(result.tier)
            return _RoutingDecision(
                provider=decision.provider,
                model=decision.model,
                tier=decision.tier,
                reason=f"llm_judge_{result.tier.lower()}"
                + ("_cached" if result.cached else ""),
            )

        # Heuristic routing (default)
        return self._select_model_for_turn(
            user_message=user_message,
            history=history,
            tools_count=tools_count,
        )

    def _tier_to_routing_decision(self, tier: str) -> _RoutingDecision:
        """Map a classifier tier to a routing decision using configured models."""
        agent_cfg = self._settings.agent
        fast_provider = agent_cfg.fast_provider or agent_cfg.provider
        fast_model = agent_cfg.fast_model or agent_cfg.haiku_model or agent_cfg.model
        strong_provider = agent_cfg.strong_provider or agent_cfg.provider
        strong_model = agent_cfg.strong_model or agent_cfg.model

        if tier == "FAST":
            return _RoutingDecision(fast_provider, fast_model, "fast", "llm_judge")
        if tier == "STRONG":
            return _RoutingDecision(strong_provider, strong_model, "strong", "llm_judge")
        return _RoutingDecision(agent_cfg.provider, agent_cfg.model, "default", "llm_judge")

    def _select_model_for_turn(
        self,
        *,
        user_message: str,
        history: list[dict[str, Any]],
        tools_count: int,
    ) -> _RoutingDecision:
        def has_keyword(text: str, keyword: str) -> bool:
            if " " in keyword or "-" in keyword:
                return keyword in text
            return re.search(rf"\b{re.escape(keyword)}\b", text) is not None

        agent_cfg = self._settings.agent
        if not agent_cfg.routing_enabled:
            return _RoutingDecision(
                provider=agent_cfg.provider,
                model=agent_cfg.model,
                tier="default",
                reason="routing_disabled",
            )

        msg = user_message.strip().lower()

        def is_command_like(text: str) -> bool:
            natural_language_cues = {
                "what",
                "why",
                "how",
                "when",
                "should",
                "explica",
                "dime",
                "deci",
                "para",
                "que",
                "cual",
                "frase",
                "sentence",
                "sirve",
                "nombra",
                "comando",
            }
            if any(has_keyword(text, cue) for cue in natural_language_cues):
                return False
            return re.fullmatch(r"[a-z0-9_.:/-]+(?:\s+[a-z0-9_.:/-]+){0,2}", text) is not None

        strong_keywords = {
            "analiza",
            "analyze",
            "refactor",
            "refactoriza",
            "debug",
            "investiga",
            "investigar",
            "compare",
            "compara",
            "diseña",
            "design",
            "arquitectura",
            "architecture",
            "worker",
            "workers",
            "orquestador",
            "orchestrator",
            "multi-step",
            "multistep",
        }
        explicit_deep_phrases = {
            "analisis profundo",
            "análisis profundo",
            "in-depth",
            "deep analysis",
            "root cause",
            "causa raiz",
            "causa raíz",
            "paso a paso",
        }
        fast_keywords = {
            "ls",
            "pwd",
            "date",
            "whoami",
            "status",
            "estado",
            "listar",
            "mostra",
            "mostrar",
        }

        fast_provider = agent_cfg.fast_provider or agent_cfg.provider
        fast_model = agent_cfg.fast_model or agent_cfg.haiku_model or agent_cfg.model
        strong_provider = agent_cfg.strong_provider or agent_cfg.provider
        strong_model = agent_cfg.strong_model or agent_cfg.model

        if (
            len(msg) <= 24
            and is_command_like(msg)
            and any(has_keyword(msg, keyword) for keyword in fast_keywords)
        ):
            return _RoutingDecision(fast_provider, fast_model, "fast", "fast_keyword")

        matched_strong_keywords = [k for k in strong_keywords if has_keyword(msg, k)]
        keyword_signal_score = (
            2 if len(matched_strong_keywords) >= 2 else 1 if matched_strong_keywords else 0
        )
        has_explicit_deep_signal = any(phrase in msg for phrase in explicit_deep_phrases)
        long_prompt_signal = len(msg) >= 280
        long_history_signal = len(history) >= 10
        strong_signal_score = (
            keyword_signal_score
            + (1 if has_explicit_deep_signal else 0)
            + (1 if long_prompt_signal else 0)
            + (1 if long_history_signal else 0)
        )
        if strong_signal_score >= 2:
            if keyword_signal_score >= 2:
                reason = "strong_keyword_combo"
            elif has_explicit_deep_signal:
                reason = "strong_explicit_deep"
            elif long_prompt_signal:
                reason = "strong_long_prompt"
            else:
                reason = "strong_long_history"
            return _RoutingDecision(strong_provider, strong_model, "strong", reason)

        if len(history) >= 14 or tools_count >= 12:
            return _RoutingDecision(
                agent_cfg.provider, agent_cfg.model, "default", "context_or_tools_v2"
            )

        return _RoutingDecision(agent_cfg.provider, agent_cfg.model, "default", "default")

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
            for block, result in zip(tier1_blocks, results, strict=False):
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

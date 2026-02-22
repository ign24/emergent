"""Interactive terminal chat channel for Emergent."""

from __future__ import annotations

import asyncio
import time

import structlog
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from emergent.agent.context import ContextBuilder
from emergent.agent.runtime import AgentRuntime
from emergent.config import EmergentSettings
from emergent.memory.store import MemoryStore
from emergent.memory.summarizer import summarize_conversation

logger = structlog.get_logger(__name__)

_ACCENT = "#7C3AED"
_DIM = "grey50"
_WARN = "yellow"

_EXIT_COMMANDS = frozenset({"exit", "quit", "q"})

SESSION_ID = "terminal_session"


class TerminalChannel:
    """Interactive terminal chat — runs the same pipeline as TelegramGateway."""

    def __init__(
        self,
        settings: EmergentSettings,
        runtime: AgentRuntime,
        store: MemoryStore,
        context_builder: ContextBuilder,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._store = store
        self._context_builder = context_builder
        self._console = Console(highlight=False)
        self._running = False
        self._input_future: asyncio.Future[str] | None = None

    async def start(self) -> None:
        """Main input loop — blocks until exit/quit/Ctrl+C/EOF."""
        self._running = True
        loop = asyncio.get_running_loop()

        self._console.print(
            f"  [{_DIM}]Terminal chat ready. Type exit or Ctrl+C to quit.[/]\n"
        )

        while self._running:
            try:
                self._input_future = loop.run_in_executor(
                    None, input, "you \u203a "
                )
                user_text: str = await self._input_future
            except (EOFError, asyncio.CancelledError):
                break

            user_text = user_text.strip()
            if not user_text:
                continue
            if user_text.lower() in _EXIT_COMMANDS:
                break

            await self._process_message(user_text)

        self._running = False

    async def stop(self) -> None:
        """Cancel the pending input future so the loop exits."""
        self._running = False
        if self._input_future and not self._input_future.done():
            self._input_future.cancel()

    async def _process_message(self, user_text: str) -> None:
        log = logger.bind(session_id=SESSION_ID)
        log.info("terminal_message_received", message_len=len(user_text))

        # Build context from memory
        try:
            profile_text, memories, summary, history = (
                await self._context_builder.build_context(
                    session_id=SESSION_ID,
                    current_query=user_text,
                )
            )
        except Exception as e:
            log.error("context_build_failed", error=str(e))
            profile_text, memories, summary, history = None, None, None, []

        # Auto-summarization if needed
        if self._context_builder.should_summarize(history):
            try:
                import anthropic

                client = anthropic.AsyncAnthropic(
                    api_key=self._settings.anthropic_api_key
                )
                new_summary = await summarize_conversation(
                    client, history, haiku_model=self._settings.agent.haiku_model
                )
                if new_summary:
                    await self._store.save_session_summary(SESSION_ID, new_summary)
                    summary = new_summary
                    history = history[-5:]
                    log.info(
                        "auto_summarization_done", summary_len=len(new_summary)
                    )
            except Exception as e:
                log.error("auto_summarization_failed", error=str(e))

        # Thinking indicator
        self._console.print(f"  [{_DIM}]\u25cf thinking...[/]")

        # Run agent
        t0 = time.monotonic()
        try:
            response_text, trace_data = await self._runtime.run(
                user_message=user_text,
                session_id=SESSION_ID,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
                confirm_callback=self._confirm,
            )
        except Exception as e:
            log.error("runtime_error", error=str(e))
            self._console.print(
                f"  [red]\u2717[/] [red]error:[/] [{_DIM}]{e}[/]"
            )
            return

        elapsed = time.monotonic() - t0
        tokens = (
            trace_data.get("total_tokens", 0)
            if isinstance(trace_data, dict)
            else 0
        )

        # Persist conversation
        try:
            await self._store.save_conversation_turn(SESSION_ID, "user", user_text)
            await self._store.save_conversation_turn(
                SESSION_ID, "assistant", response_text
            )
            await self._store.save_trace(trace_data)
        except Exception as e:
            log.error("persistence_failed", error=str(e))

        # ChromaDB upsert (fire-and-forget)
        asyncio.create_task(
            self._context_builder._retriever.upsert_session(
                session_id=SESSION_ID,
                turns=[
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": response_text},
                ],
            )
        )

        # Render response
        panel = Panel(
            Markdown(response_text),
            border_style=_ACCENT,
            expand=True,
        )
        self._console.print(panel)
        self._console.print(
            f"  [{_DIM}]\u21b3 {elapsed:.1f}s \u00b7 {tokens:,} tokens[/]\n"
        )

    async def _confirm(self, tool_name: str, command_preview: str) -> bool:
        """TIER_2 interactive confirmation via terminal input."""
        self._console.print()
        self._console.print(f"  [{_WARN}]\u26a0 Confirmation required[/]")
        self._console.print(f"  [{_DIM}]Tool:[/]    [white]{tool_name}[/]")
        self._console.print(f"  [{_DIM}]Command:[/] [white]{command_preview}[/]")

        loop = asyncio.get_running_loop()
        timeout = self._settings.agent.CONFIRMATION_TIMEOUT_SECONDS

        try:
            answer: str = await asyncio.wait_for(
                loop.run_in_executor(None, input, "  Allow? [y/N] \u203a "),
                timeout=timeout,
            )
        except TimeoutError:
            self._console.print(f"  [{_DIM}]Timed out. Operation cancelled.[/]\n")
            return False
        except (EOFError, asyncio.CancelledError):
            return False

        return answer.strip().lower() in ("y", "yes")

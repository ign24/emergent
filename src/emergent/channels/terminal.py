"""Interactive terminal chat channel for Emergent."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from statistics import mean
from typing import Any

import structlog
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from emergent.agent.context import ContextBuilder
from emergent.agent.runtime import AgentRuntime
from emergent.config import EmergentSettings
from emergent.llm.factory import create_llm_client
from emergent.memory.store import MemoryStore
from emergent.memory.summarizer import summarize_conversation

logger = structlog.get_logger(__name__)

_ACCENT = "#7C3AED"
_DIM = "grey50"
_WARN = "yellow"

_EXIT_COMMANDS = frozenset({"exit", "quit", "q"})
SESSION_ID = "terminal_session"


@dataclass(frozen=True)
class _SkillPreset:
    name: str
    description: str
    instruction: str


_SKILL_PRESETS: dict[str, _SkillPreset] = {
    "brainstorming": _SkillPreset(
        name="brainstorming",
        description="Explora opciones y trade-offs antes de implementar.",
        instruction=(
            "Actua en modo brainstorming. Antes de proponer implementacion, "
            "presenta 2-3 enfoques con trade-offs y una recomendacion."
        ),
    ),
    "debugger": _SkillPreset(
        name="debugger",
        description="Diagnostico sistematico de bugs y causa raiz.",
        instruction=(
            "Actua en modo debugger. Identifica sintomas, causa raiz probable, "
            "pasos de reproduccion y fix minimo verificable."
        ),
    ),
    "code-reviewer": _SkillPreset(
        name="code-reviewer",
        description="Review centrado en seguridad, performance y claridad.",
        instruction=(
            "Actua en modo code reviewer. Evalua riesgos de seguridad, performance, "
            "mantenibilidad y sugiere mejoras accionables."
        ),
    ),
    "technical-writer": _SkillPreset(
        name="technical-writer",
        description="Redaccion tecnica clara y estructurada.",
        instruction=(
            "Actua en modo technical writer. Responde con estructura clara, "
            "lenguaje directo y ejemplos minimos cuando ayuden."
        ),
    ),
    "api-design": _SkillPreset(
        name="api-design",
        description="Disena APIs REST claras y consistentes.",
        instruction=(
            "Actua en modo API design. Prioriza recursos bien nombrados, "
            "status codes correctos, validaciones y errores consistentes."
        ),
    ),
}


@dataclass
class _SessionStats:
    started_at_monotonic: float
    user_turns: int = 0
    assistant_turns: int = 0
    error_turns: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    latencies_s: list[float] | None = None
    last_latency_s: float = 0.0
    last_tokens: int = 0

    def __post_init__(self) -> None:
        if self.latencies_s is None:
            self.latencies_s = []


class TerminalChannel:
    """Interactive terminal chat — runs the same pipeline as TelegramGateway."""

    def __init__(
        self,
        settings: EmergentSettings,
        runtime: AgentRuntime,
        store: MemoryStore,
        context_builder: ContextBuilder,
        scheduler_jobs: int = 0,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._store = store
        self._context_builder = context_builder
        self._console = Console(highlight=False)
        self._running = False
        self._input_future: asyncio.Future[str] | None = None
        self._live: Live | None = None
        self._scheduler_jobs = max(0, scheduler_jobs)
        self._pending_escape_presses = 0
        self._active_skill: str | None = None
        self._stats = _SessionStats(started_at_monotonic=time.monotonic())

        # Exposed fields used by tests and quick introspection
        self._user_turns = 0
        self._assistant_turns = 0
        self._errors = 0
        self._total_tokens = 0
        self._tool_calls = 0
        self._last_latency_s = 0.0
        self._last_tokens = 0

    async def start(self) -> None:
        """Main input loop — blocks until exit/quit/Ctrl+C/EOF."""
        self._running = True
        loop = asyncio.get_running_loop()

        self._console.print(f"  [{_DIM}]Terminal chat ready. Type exit or Ctrl+C to quit.[/]")
        self._live = Live(
            self._build_live_panel(),
            console=self._console,
            auto_refresh=False,
            transient=False,
        )
        self._live.start()

        try:
            while self._running:
                try:
                    self._input_future = loop.run_in_executor(
                        None,
                        self._console.input,
                        "you \u203a ",
                    )
                    user_text: str = await self._input_future
                except (EOFError, asyncio.CancelledError):
                    break

                user_text = user_text.strip()
                if not user_text:
                    continue
                if self._register_escape_press(user_text):
                    break
                if user_text.lower() in _EXIT_COMMANDS:
                    break
                if await self._handle_local_command(user_text):
                    continue

                await self._process_message(user_text)
        finally:
            if self._live is not None:
                self._live.stop()
                self._live = None

        self._running = False

    async def stop(self) -> None:
        """Cancel the pending input future so the loop exits."""
        self._running = False
        if self._input_future and not self._input_future.done():
            self._input_future.cancel()

    async def _process_message(self, user_text: str, skill_override: str | None = None) -> None:
        log = logger.bind(session_id=SESSION_ID)
        log.info("terminal_message_received", message_len=len(user_text))
        self._stats.user_turns += 1
        self._sync_stat_fields()
        runtime_user_text = self._build_runtime_message(user_text, skill_override=skill_override)

        # Build context from memory
        try:
            profile_text, memories, summary, history = await self._context_builder.build_context(
                session_id=SESSION_ID,
                current_query=user_text,
            )
        except Exception as e:
            log.error("context_build_failed", error=str(e))
            profile_text, memories, summary, history = None, None, None, []

        # Auto-summarization if needed
        if self._context_builder.should_summarize(history):
            try:
                client = create_llm_client(self._settings, self._settings.agent.summary_provider)
                try:
                    new_summary = await summarize_conversation(
                        client, history, summary_model=self._settings.agent.haiku_model
                    )
                finally:
                    await client.close()
                if new_summary:
                    await self._store.save_session_summary(SESSION_ID, new_summary)
                    summary = new_summary
                    history = history[-5:]
                    log.info("auto_summarization_done", summary_len=len(new_summary))
            except Exception as e:
                log.error("auto_summarization_failed", error=str(e))

        # Thinking indicator
        self._console.print(f"  [{_DIM}]assistant is thinking...[/]")

        # Run agent
        t0 = time.monotonic()
        try:
            response_text, trace_data = await self._runtime.run(
                user_message=runtime_user_text,
                session_id=SESSION_ID,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
                confirm_callback=self._confirm,
            )
        except Exception as e:
            log.error("runtime_error", error=str(e))
            self._stats.error_turns += 1
            self._sync_stat_fields()
            self._refresh_live_once()
            self._console.print(f"  [red]\u2717[/] [red]error:[/] [{_DIM}]{e}[/]")
            return

        elapsed = time.monotonic() - t0
        tokens = self._extract_total_tokens(trace_data)
        self._record_stats_after_response(elapsed_s=elapsed, tokens=tokens, trace_data=trace_data)
        self._refresh_live_once()

        # Persist conversation
        try:
            await self._store.save_conversation_turn(SESSION_ID, "user", user_text)
            await self._store.save_conversation_turn(SESSION_ID, "assistant", response_text)
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

        # Render response in a clean transcript style
        self._console.print("assistant ›")
        self._console.print(Markdown(response_text))
        self._console.print(f"  [{_DIM}]\u21b3 {elapsed:.1f}s \u00b7 {tokens:,} tokens[/]\n")

    def _extract_total_tokens(self, trace_data: Any) -> int:
        """Get total tokens from trace payload (backward compatible)."""
        if not isinstance(trace_data, dict):
            return 0
        explicit = trace_data.get("total_tokens")
        if isinstance(explicit, int):
            return explicit

        input_tokens = trace_data.get("total_input_tokens", 0)
        output_tokens = trace_data.get("total_output_tokens", 0)
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            return input_tokens + output_tokens
        return 0

    def _record_stats_after_response(self, elapsed_s: float, tokens: int, trace_data: Any) -> None:
        """Update terminal session dashboard counters."""
        self._stats.assistant_turns += 1
        self._stats.total_tokens += max(0, tokens)
        self._stats.last_tokens = max(0, tokens)
        self._stats.last_latency_s = max(0.0, elapsed_s)
        if self._stats.latencies_s is not None:
            self._stats.latencies_s.append(max(0.0, elapsed_s))

        if isinstance(trace_data, dict):
            tools_called = trace_data.get("tools_called", [])
            if isinstance(tools_called, list):
                self._stats.tool_calls += len(tools_called)

        self._sync_stat_fields()

    def _sync_stat_fields(self) -> None:
        """Keep simple counters available for tests and debugging."""
        self._user_turns = self._stats.user_turns
        self._assistant_turns = self._stats.assistant_turns
        self._errors = self._stats.error_turns
        self._total_tokens = self._stats.total_tokens
        self._tool_calls = self._stats.tool_calls
        self._last_latency_s = self._stats.last_latency_s
        self._last_tokens = self._stats.last_tokens

    def set_scheduler_jobs(self, jobs_count: int) -> None:
        """Set scheduler jobs count for live dashboard context."""
        self._scheduler_jobs = max(0, jobs_count)
        self._refresh_live_once()

    async def _handle_local_command(self, text: str) -> bool:
        """Handle terminal-only slash commands. Returns True if consumed."""
        raw = text.strip()
        if not raw.startswith("/"):
            return False

        lower = raw.lower()
        if lower in {"/skills", "/skill"}:
            self._print_skills_help()
            return True

        if lower.startswith("/skill "):
            arg = raw.split(maxsplit=1)[1].strip().lower()
            if arg in {"off", "none", "clear"}:
                self._active_skill = None
                self._console.print(f"  [{_DIM}]Skill mode disabled.[/]")
                return True
            if arg in _SKILL_PRESETS:
                self._active_skill = arg
                self._console.print(f"  [{_DIM}]Skill mode enabled:[/] [white]{arg}[/]")
                return True
            self._console.print(f"  [yellow]Unknown skill:[/] {arg}")
            self._print_skills_help()
            return True

        parts = raw.split(maxsplit=1)
        command = parts[0][1:].lower()
        if command in _SKILL_PRESETS:
            if len(parts) == 1 or not parts[1].strip():
                self._console.print(f"  [{_DIM}]Usage:[/] /{command} <mensaje>")
                return True
            await self._process_message(parts[1].strip(), skill_override=command)
            return True

        return False

    def _print_skills_help(self) -> None:
        """Render available skill presets and usage."""
        table = Table(show_header=True, header_style="bold", expand=False)
        table.add_column("skill")
        table.add_column("descripcion")
        for skill in _SKILL_PRESETS.values():
            table.add_row(f"/{skill.name}", skill.description)

        self._console.print(table)
        active = self._active_skill or "none"
        self._console.print(
            f"  [{_DIM}]Use /skill <name> para modo persistente, "
            "/skill off para desactivar, /<skill> <mensaje> para one-shot. "
            f"Activa:[/] {active}"
        )

    def _build_runtime_message(self, user_text: str, skill_override: str | None = None) -> str:
        """Wrap user text with optional skill instructions for runtime."""
        chosen = skill_override or self._active_skill
        if chosen is None:
            return user_text
        preset = _SKILL_PRESETS.get(chosen)
        if preset is None:
            return user_text
        return (
            f"[Skill activo: {preset.name}]\n"
            f"{preset.instruction}\n\n"
            f"Solicitud del usuario:\n{user_text}"
        )

    def _register_escape_press(self, text: str) -> bool:
        """Exit when escape is submitted twice in a row."""
        if text == "\x1b":
            self._pending_escape_presses += 1
            if self._pending_escape_presses >= 2:
                return True
            self._console.print(f"  [{_DIM}]Press ESC again to quit.[/]")
            return False

        self._pending_escape_presses = 0
        return False

    def _build_live_panel(self) -> Panel:
        """Build persistent mini dashboard shown above the prompt."""
        uptime_s = max(0.0, time.monotonic() - self._stats.started_at_monotonic)
        avg_latency = mean(self._stats.latencies_s) if self._stats.latencies_s else 0.0

        table = Table.grid(expand=True)
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_column(justify="left")
        table.add_row(
            f"[bold]jobs[/] {self._scheduler_jobs}",
            f"[bold]turnos[/] {self._stats.user_turns}/{self._stats.assistant_turns}",
            f"[bold]tokens[/] {self._stats.total_tokens:,}",
            f"[bold]tools[/] {self._stats.tool_calls}",
            f"[bold]lat[/] {avg_latency:.1f}s · [bold]up[/] {uptime_s:.0f}s",
            f"[bold]skill[/] {self._active_skill or '-'}",
        )

        return Panel(
            table,
            border_style=_ACCENT,
            title="live dashboard",
            expand=True,
        )

    def _refresh_live_once(self) -> None:
        """Refresh live mini dashboard if active."""
        if self._live is not None:
            self._live.update(self._build_live_panel(), refresh=True)

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
                loop.run_in_executor(None, self._console.input, "  Allow? [y/N] \u203a "),
                timeout=timeout,
            )
        except TimeoutError:
            self._console.print(f"  [{_DIM}]Timed out. Operation cancelled.[/]\n")
            return False
        except (EOFError, asyncio.CancelledError):
            return False

        return answer.strip().lower() in ("y", "yes")

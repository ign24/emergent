"""Interactive terminal chat channel for Emergent."""

from __future__ import annotations

import asyncio
import re
import signal
import sys
import termios
import time
import tty
import uuid
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

_ACCENT = "white"
_CYAN = "#06B6D4"
_DIM = "grey50"
_WARN = "yellow"

_EXIT_COMMANDS = frozenset({"exit", "quit", "q"})
DEFAULT_SESSION_ID = "terminal"
_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")
_CONFIRM_ALLOW_KEYS = frozenset({"y", "s"})
_CONFIRM_DENY_KEYS = frozenset({"n", "\r", "\n", "\x1b"})
_ASSISTANT_SPINNER_FRAMES = (
    "[.  ]",
    "[.. ]",
    "[...]",
    "[ ..]",
    "[  .]",
)
_ASSISTANT_STATE_STYLES: dict[str, str] = {
    "idle": "green",
    "writing": "cyan",
    "thinking": "cyan",
    "tool": "bright_cyan",
    "error": "red",
}
_VOICE_STATE_STYLES: dict[str, str] = {
    "off": "grey50",
    "starting": "cyan",
    "ready": "green",
    "listening": "bright_green",
    "transcribing": "yellow",
    "thinking": "blue",
    "responded": "green",
    "speaking": "cyan",
    "stopped": "grey50",
    "error": "red",
}


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
    llm_mode: str = "AUTO"
    last_model_tier: str = "-"
    last_model_name: str = "-"
    session_cost_usd: float = 0.0
    last_cost_usd: float = 0.0

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
        self._supports_live_ui = bool(getattr(self._console, "is_terminal", False)) and not bool(
            getattr(self._console, "is_dumb_terminal", False)
        )
        self._running = False
        self._input_future: asyncio.Future[str] | None = None
        self._live: Live | None = None
        self._interrupted = False
        self._scheduler_jobs = max(0, scheduler_jobs)
        self._session_id = f"{DEFAULT_SESSION_ID}_{uuid.uuid4().hex[:8]}"
        self._session_choices: list[str] | None = None
        self._pending_escape_presses = 0
        self._active_skill: str | None = None
        self._dashboard_enabled = False
        self._stats = _SessionStats(started_at_monotonic=time.monotonic())
        self._stats.llm_mode = "AUTO" if settings.agent.routing_enabled else "FIXED"
        self._assistant_state = "idle"
        self._assistant_status = "listo"
        self._assistant_animating = False
        self._assistant_spinner_index = 0
        self._assistant_anim_task: asyncio.Task[None] | None = None
        self._streaming_response = False
        self._voice_state = "off"
        self._voice_status = "desactivado"
        self._voice_channel: Any | None = None

        # Exposed fields used by tests and quick introspection
        self._user_turns = 0
        self._assistant_turns = 0
        self._errors = 0
        self._total_tokens = 0
        self._tool_calls = 0
        self._last_latency_s = 0.0
        self._last_tokens = 0

    def set_voice_status(self, state: str, status: str) -> None:
        """Update live dashboard with latest voice channel status."""
        normalized_state = (state or "unknown").strip().lower()
        normalized_status = (status or "").strip() or "-"
        self._voice_state = normalized_state
        self._voice_status = normalized_status
        self._refresh_live_once()

    def set_voice_channel(self, voice_channel: Any | None) -> None:
        """Attach voice channel to enable /voice local command."""
        self._voice_channel = voice_channel

    def _set_assistant_status(self, state: str, status: str, animated: bool = False) -> None:
        """Update assistant activity indicator in live dashboard."""
        self._assistant_state = (state or "idle").strip().lower()
        self._assistant_status = (status or "-").strip() or "-"
        if animated and not self._supports_live_ui:
            animated = False
        self._assistant_animating = animated
        if animated:
            self._start_assistant_animation()
        else:
            self._stop_assistant_animation()
            self._assistant_spinner_index = 0
        self._refresh_live_once()

    def _start_assistant_animation(self) -> None:
        """Start periodic dashboard refresh while assistant is busy."""
        if self._assistant_anim_task is not None and not self._assistant_anim_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._assistant_anim_task = loop.create_task(
            self._assistant_animation_loop(),
            name="terminal_assistant_animation",
        )

    def _stop_assistant_animation(self) -> None:
        """Stop active assistant animation task if running."""
        task = self._assistant_anim_task
        self._assistant_anim_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _assistant_animation_loop(self) -> None:
        """Advance spinner frames while assistant activity is animated."""
        try:
            while self._running and self._assistant_animating:
                self._assistant_spinner_index = (self._assistant_spinner_index + 1) % len(
                    _ASSISTANT_SPINNER_FRAMES
                )
                self._refresh_live_once()
                await asyncio.sleep(0.11)
        except asyncio.CancelledError:
            return

    def _build_assistant_status_text(self, width: int) -> str:
        """Render assistant status with subtle animation and state color."""
        state_style = _ASSISTANT_STATE_STYLES.get(self._assistant_state, "white")
        state_label = f"[{state_style}]{self._assistant_state}[/]"
        if self._assistant_animating:
            frame = _ASSISTANT_SPINNER_FRAMES[self._assistant_spinner_index]
            assistant_prefix = f"[bright_cyan]{frame}[/] "
        else:
            assistant_prefix = ""
        truncated_status = self._truncate_for_width(
            self._assistant_status, width=width, role="assistant"
        )
        return f"{assistant_prefix}{state_label}: {truncated_status}"

    async def start(self) -> None:
        """Main input loop — blocks until exit/quit/Ctrl+C/EOF."""
        self._running = True
        self._interrupted = False
        loop = asyncio.get_running_loop()
        sigint_handler_installed = False

        try:
            loop.add_signal_handler(signal.SIGINT, self._handle_sigint_signal)
            sigint_handler_installed = True
        except (NotImplementedError, RuntimeError):
            sigint_handler_installed = False

        self._console.print(
            f"  [{_DIM}]Terminal chat ready. Type [{_CYAN}]exit[/] "
            f"or [{_CYAN}]Ctrl+C[/] to quit.[/]"
        )
        self._console.print(
            f"  [{_DIM}]Atajos:[/] "
            f"[{_CYAN}]ESC ESC[/] [{_DIM}]salir[/], "
            f"[{_CYAN}]/skills[/] [{_DIM}]ver modos[/], "
            f"[{_CYAN}]/session[/] [{_DIM}]listar y volver a sesion[/], "
            f"[{_CYAN}]/session <id>[/] [{_DIM}]cambiar[/], "
            f"[{_CYAN}]/voice[/] [{_DIM}]continuo[/], "
            f"[{_CYAN}]/voice-off[/] [{_DIM}]salir[/], "
            f"[{_CYAN}]/voice [seg][/] [{_DIM}]puntual[/], "
            f"[{_CYAN}]/panel on|off[/] [{_DIM}]dashboard[/]."
        )
        try:
            await self._store.ensure_session(self._session_id, channel="terminal")
        except Exception as e:
            logger.warning(
                "terminal_session_ensure_failed", session_id=self._session_id, error=str(e)
            )
        if self._dashboard_enabled:
            self._start_live_dashboard()

        try:
            while self._running:
                try:
                    self._input_future = loop.run_in_executor(
                        None,
                        self._console.input,
                        "you \u203a ",
                    )
                    user_text: str = await self._input_future
                except KeyboardInterrupt:
                    self._handle_sigint_signal()
                    break
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
            self._stop_assistant_animation()
            if sigint_handler_installed:
                loop.remove_signal_handler(signal.SIGINT)
            if self._live is not None:
                self._live.stop()
                self._live = None

        self._running = False

    async def stop(self) -> None:
        """Cancel the pending input future so the loop exits."""
        self._running = False
        self._stop_assistant_animation()
        if self._input_future and not self._input_future.done():
            self._input_future.cancel()

    def _handle_sigint_signal(self) -> None:
        """Handle Ctrl+C consistently by stopping pending terminal input."""
        if self._interrupted:
            return
        self._interrupted = True
        self._running = False
        if self._input_future is not None and not self._input_future.done():
            self._input_future.cancel()
        self._console.print(f"\n  [{_DIM}]Interrupcion recibida (Ctrl+C), cerrando...[/]")

    async def _process_message(self, user_text: str, skill_override: str | None = None) -> None:
        log = logger.bind(session_id=self._session_id)
        log.info("terminal_message_received", message_len=len(user_text))
        self._stats.user_turns += 1
        self._sync_stat_fields()
        runtime_user_text = self._build_runtime_message(user_text, skill_override=skill_override)

        try:
            await self._store.auto_name_session_from_first_prompt(
                self._session_id,
                user_text,
                channel="terminal",
            )
        except Exception as e:
            log.warning("terminal_session_autoname_failed", error=str(e))

        # Build context from memory
        try:
            profile_text, memories, summary, history = await self._context_builder.build_context(
                session_id=self._session_id,
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
                    await self._store.save_session_summary(self._session_id, new_summary)
                    summary = new_summary
                    keep_turns = getattr(self._context_builder, "history_keep_after_summary", 5)
                    history = history[-keep_turns:]
                    log.info("auto_summarization_done", summary_len=len(new_summary))
            except Exception as e:
                log.error("auto_summarization_failed", error=str(e))

        # Thinking indicator
        self._set_assistant_status("writing", "escribiendo...", animated=True)

        streamed_started = False

        async def on_text_delta(delta: str) -> None:
            nonlocal streamed_started
            if not delta:
                return
            if not streamed_started:
                if self._live is not None:
                    self._live.console.print("assistant ›")
                else:
                    self._console.print("assistant ›")
                streamed_started = True
                self._streaming_response = True
                if self._assistant_animating:
                    self._assistant_animating = False
                    self._stop_assistant_animation()
            if self._live is not None:
                self._live.console.print(
                    delta, end="", markup=False, highlight=False, soft_wrap=True
                )
            else:
                self._console.print(delta, end="", markup=False, highlight=False, soft_wrap=True)

        # Run agent
        t0 = time.monotonic()
        try:
            response_text, trace_data = await self._runtime.run(
                user_message=runtime_user_text,
                session_id=self._session_id,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
                confirm_callback=self._confirm,
                on_text_delta=on_text_delta,
            )
        except Exception as e:
            self._streaming_response = False
            log.error("runtime_error", error=str(e))
            self._stats.error_turns += 1
            self._sync_stat_fields()
            self._refresh_live_once()
            self._set_assistant_status("error", "fallo", animated=False)
            self._console.print(f"  [red]\u2717[/] [red]error:[/] [{_DIM}]{e}[/]")
            return

        elapsed = time.monotonic() - t0
        tokens = self._extract_total_tokens(trace_data)
        self._streaming_response = False
        self._record_stats_after_response(elapsed_s=elapsed, tokens=tokens, trace_data=trace_data)
        self._set_assistant_status("idle", "listo", animated=False)
        self._refresh_live_once()

        # Persist conversation
        try:
            await self._store.save_conversation_turn(self._session_id, "user", user_text)
            await self._store.save_conversation_turn(self._session_id, "assistant", response_text)
            await self._store.save_trace(trace_data)
        except Exception as e:
            log.error("persistence_failed", error=str(e))

        # ChromaDB upsert (fire-and-forget)
        asyncio.create_task(
            self._context_builder._retriever.upsert_session(
                session_id=self._session_id,
                turns=[
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": response_text},
                ],
            )
        )

        # Render response in a clean transcript style
        if streamed_started:
            self._console.print()
        else:
            self._console.print("assistant ›")
            self._console.print(Markdown(response_text))
        cost_str = f"${self._stats.last_cost_usd:.4f}"
        session_cost_str = f"${self._stats.session_cost_usd:.4f}"
        self._console.print(
            f"  [{_DIM}]\u21b3 {elapsed:.1f}s \u00b7 {tokens:,} tokens \u00b7 "
            f"{cost_str} (session {session_cost_str}) \u00b7 "
            f"{self._stats.llm_mode}:{self._stats.last_model_tier} \u00b7 "
            f"{self._stats.last_model_name}[/]\n"
        )

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

            model_tier = trace_data.get("model_tier")
            if isinstance(model_tier, str) and model_tier:
                self._stats.last_model_tier = model_tier

            model_name = trace_data.get("model")
            if isinstance(model_name, str) and model_name:
                self._stats.last_model_name = model_name

            session_cost = trace_data.get("session_cost_usd")
            if isinstance(session_cost, (int, float)):
                self._stats.session_cost_usd = float(session_cost)

            turn_cost = trace_data.get("total_cost_usd")
            if isinstance(turn_cost, (int, float)):
                self._stats.last_cost_usd = float(turn_cost)

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

        if lower == "/session":
            await self._show_session_selector()
            return True

        if lower == "/panel":
            state = "on" if self._dashboard_enabled else "off"
            self._console.print(
                f"  [{_DIM}]Dashboard:[/] [white]{state}[/] "
                "([white]/panel on[/] o [white]/panel off[/])."
            )
            return True

        if lower in {"/panel on", "/dashboard on"}:
            self._dashboard_enabled = True
            if self._running:
                self._start_live_dashboard()
            if self._supports_live_ui:
                self._console.print(f"  [{_DIM}]Dashboard activado.[/]")
            else:
                self._console.print(
                    f"  [{_DIM}]Dashboard limitado:[/] este terminal no soporta refresco en vivo."
                )
            return True

        if lower in {"/panel off", "/dashboard off"}:
            self._dashboard_enabled = False
            if self._running:
                self._stop_live_dashboard()
            self._console.print(f"  [{_DIM}]Dashboard desactivado.[/]")
            return True

        if lower.startswith("/session "):
            arg = raw.split(maxsplit=1)[1].strip()
            if arg.isdigit() and self._session_choices:
                index = int(arg) - 1
                if index < 0 or index >= len(self._session_choices):
                    self._console.print("  [yellow]Seleccion fuera de rango.[/] Proba de nuevo.")
                    return True
                self._session_id = self._session_choices[index]
                self._session_choices = None
                self._console.print(f"  [{_DIM}]Session set:[/] [white]{self._session_id}[/]")
                self._refresh_live_once()
                return True

            if arg.lower() in {"reset", "default"}:
                self._session_id = f"{DEFAULT_SESSION_ID}_{uuid.uuid4().hex[:8]}"
                self._session_choices = None
                self._console.print(f"  [{_DIM}]Session reset:[/] [white]{self._session_id}[/]")
                self._refresh_live_once()
                return True

            if not _SESSION_ID_PATTERN.match(arg):
                self._console.print(
                    "  [yellow]Invalid session id.[/] Use 3-64 chars: letters, numbers, '_' or '-'."
                )
                return True

            self._session_id = arg
            self._session_choices = None
            self._console.print(f"  [{_DIM}]Session set:[/] [white]{self._session_id}[/]")
            self._refresh_live_once()
            return True

        if lower == "/voice" or lower.startswith("/voice ") or lower == "/voice-off":
            if self._voice_channel is None:
                self._console.print(
                    "  [yellow]Voice no esta disponible.[/] "
                    "Activa [white]voice.enabled[/] en config.yaml"
                )
                return True

            if not bool(getattr(self._voice_channel, "enabled", False)):
                self._console.print(
                    "  [yellow]Voice desactivado.[/] Configura [white]voice.enabled: true[/]"
                )
                return True

            if lower in {"/voice-off", "/voice off"}:
                await self._voice_channel.stop_continuous()
                self._console.print(f"  [{_DIM}]Modo voz continuo desactivado.[/]")
                return True

            if lower == "/voice":
                await self._voice_channel.start_continuous()
                self._console.print(
                    f"  [{_DIM}]Modo voz continuo activo. Usa [white]/voice-off[/] para volver.[/]"
                )
                return True

            duration_seconds: int | None = None
            if lower.startswith("/voice "):
                arg = raw.split(maxsplit=1)[1].strip()
                if arg.lower() == "off":
                    await self._voice_channel.stop_continuous()
                    self._console.print(f"  [{_DIM}]Modo voz continuo desactivado.[/]")
                    return True
                if arg:
                    if not arg.isdigit():
                        self._console.print(
                            "  [yellow]Uso:[/] /voice  | /voice-off | /voice [segundos]"
                        )
                        return True
                    duration_seconds = int(arg)

            seconds_label = duration_seconds if duration_seconds is not None else "config"
            self._console.print(f"  [{_DIM}]Modo voz: capturando un turno ({seconds_label}s).[/]")
            await self._voice_channel.capture_once(duration_seconds=duration_seconds)
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

    async def _show_session_selector(self) -> None:
        """List known sessions and allow selecting one by number."""
        self._console.print(f"  [{_DIM}]Current session:[/] [white]{self._session_id}[/]")
        try:
            rows = await self._store.get_all_sessions_with_names(limit=30)
        except Exception as e:
            logger.warning("terminal_session_list_failed", error=str(e))
            self._console.print(f"  [red]No pude cargar sesiones:[/] [{_DIM}]{e}[/]")
            return

        if not rows:
            rows = [{"session_id": self._session_id, "display_name": self._session_id, "turns": 0}]

        if all(row["session_id"] != self._session_id for row in rows):
            rows.insert(
                0, {"session_id": self._session_id, "display_name": self._session_id, "turns": 0}
            )

        if not rows:
            self._console.print(
                f"  [{_DIM}]No hay sesiones guardadas aun.[/] Usa [white]/session <id>[/]."
            )
            return

        self._session_choices = []
        for idx, row in enumerate(rows, start=1):
            session_id = str(row["session_id"])
            display_name = str(row["display_name"])
            turns = int(row.get("turns", 0))
            marker = "*" if session_id == self._session_id else " "
            self._session_choices.append(session_id)
            if display_name != session_id:
                label = display_name
            elif len(session_id) > 18:
                label = f"{session_id[:8]}..."
            else:
                label = session_id
            self._console.print(
                f"  [{_DIM}]{idx:>2}.[/] [{_DIM}]{marker}[/] [white]{label}[/] "
                f"[{_DIM}]({turns} turnos)[/]"
            )
        self._console.print(
            f"  [{_DIM}]Usa [white]/session <numero>[/] para volver, "
            "o [white]/session <id>[/] para crear/usar manualmente.[/]"
        )

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
        self._console.print(
            f"  [{_DIM}]Voice:[/] [white]/voice[/], [white]/voice-off[/], "
            "[white]/voice <segundos>[/]"
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
        width = self._get_dashboard_width()
        voice_style = _VOICE_STATE_STYLES.get(self._voice_state, "white")
        voice_status = self._truncate_for_width(self._voice_status, width=width, role="voice")
        voice_text = f"[{voice_style}]{self._voice_state}[/]: {voice_status}"
        assistant_text = self._build_assistant_status_text(width=width)
        llm_name = self._truncate_for_width(self._stats.last_model_name, width=width, role="model")
        llm_text = f"{self._stats.llm_mode}:{self._stats.last_model_tier} · {llm_name}"
        session_text = self._truncate_for_width(self._session_id, width=width, role="session")
        latency_text = f"{avg_latency:.1f}s"
        uptime_text = f"{uptime_s:.0f}s"

        table = Table.grid(expand=True, padding=(0, 1))

        cost_text = f"${self._stats.session_cost_usd:.4f}"

        if width < 80:
            table.add_column(style=_DIM, no_wrap=True)
            table.add_column(overflow="ellipsis")
            table.add_row("estado", "")
            table.add_row("  sesion", session_text)
            table.add_row("  skill", self._active_skill or "-")
            table.add_row("  voz", voice_text)
            table.add_row("  asist", assistant_text)
            table.add_row("  llm", llm_text)
            table.add_row("  tareas", str(self._scheduler_jobs))
            table.add_row("rendimiento", "")
            table.add_row("  turnos", f"{self._stats.user_turns}/{self._stats.assistant_turns}")
            table.add_row("  tokens", f"{self._stats.total_tokens:,}")
            table.add_row("  costo", cost_text)
            table.add_row("  tools", str(self._stats.tool_calls))
            table.add_row("  lat", latency_text)
            table.add_row("  up", uptime_text)
        elif width < 120:
            table.add_column(justify="left")
            table.add_column(justify="left")
            table.add_row(
                f"[bold]estado[/] [bold]sesion[/] {session_text}",
                (
                    f"[bold]skill[/] {self._active_skill or '-'} "
                    f"· [bold]tareas[/] {self._scheduler_jobs}"
                ),
            )
            table.add_row(
                f"[bold]voz[/] {voice_text}",
                f"[bold]asist[/] {assistant_text}",
            )
            table.add_row(
                f"[bold]llm[/] {llm_text}",
                (
                    f"[bold]turnos[/] {self._stats.user_turns}/{self._stats.assistant_turns} · "
                    f"[bold]tokens[/] {self._stats.total_tokens:,} "
                    f"· [bold]costo[/] {cost_text} "
                    f"· [bold]tools[/] {self._stats.tool_calls}"
                ),
            )
            table.add_row(
                f"[bold]rendimiento[/] [bold]lat[/] {latency_text}",
                f"[bold]up[/] {uptime_text}",
            )
        else:
            table.add_column(justify="left")
            table.add_row(
                f"[bold]estado[/] [bold]sesion[/] {session_text} · [bold]skill[/] "
                f"{self._active_skill or '-'} · [bold]voz[/] {voice_text} · "
                f"[bold]asist[/] {assistant_text} · [bold]tareas[/] {self._scheduler_jobs}"
            )
            table.add_row(
                f"[bold]rendimiento[/] [bold]turnos[/] "
                f"{self._stats.user_turns}/{self._stats.assistant_turns} "
                f"· [bold]tokens[/] {self._stats.total_tokens:,} "
                f"· [bold]costo[/] {cost_text} "
                f"· [bold]tools[/] {self._stats.tool_calls} "
                f"· [bold]lat[/] {latency_text} · [bold]up[/] {uptime_text} "
                f"· [bold]llm[/] {llm_text}"
            )

        return Panel(
            table,
            border_style=_ACCENT,
            title="panel en vivo",
            expand=True,
        )

    def _get_dashboard_width(self) -> int:
        """Return current terminal width with a safe fallback."""
        try:
            width = int(self._console.size.width)
        except Exception:
            return 120
        return width if width > 0 else 120

    def _truncate_for_width(self, text: str, width: int, role: str) -> str:
        """Truncate dashboard values to keep rows readable."""
        if width < 80:
            limits = {"session": 20, "voice": 18, "assistant": 18, "model": 18}
        elif width < 120:
            limits = {"session": 28, "voice": 24, "assistant": 24, "model": 28}
        else:
            limits = {"session": 40, "voice": 34, "assistant": 34, "model": 40}

        limit = limits.get(role, 24)
        if len(text) <= limit:
            return text
        if limit <= 1:
            return "..."
        return f"{text[: limit - 3]}..."

    def _refresh_live_once(self) -> None:
        """Refresh live mini dashboard if active."""
        if self._live is not None and not self._streaming_response:
            self._live.update(self._build_live_panel(), refresh=True)

    def _start_live_dashboard(self) -> None:
        """Start the persistent live dashboard if enabled."""
        if not self._dashboard_enabled:
            return
        if not self._supports_live_ui:
            return
        if self._live is not None:
            self._refresh_live_once()
            return
        self._live = Live(
            self._build_live_panel(),
            console=self._console,
            auto_refresh=False,
            transient=False,
        )
        self._live.start()

    def _stop_live_dashboard(self) -> None:
        """Stop and hide live dashboard if active."""
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    async def _confirm(self, tool_name: str, command_preview: str) -> bool:
        """TIER_2 interactive confirmation via terminal input."""
        self._console.print()
        self._console.print(
            Panel.fit(
                "\n".join(
                    [
                        (
                            "[bold yellow]Se requiere confirmacion para ejecutar una accion "
                            "sensible.[/]"
                        ),
                        f"[{_DIM}]Tool:[/] [white]{tool_name}[/]",
                        f"[{_DIM}]Command:[/] [white]{command_preview}[/]",
                        "",
                        "[bold #06B6D4]Teclado:[/] [#22C55E]Y[/]/[#22C55E]S[/] permitir  "
                        "[red]N[/] bloquear  [white]Enter[/] bloquear  [white]Esc[/] bloquear",
                    ]
                ),
                border_style=_WARN,
                title="solicitud de permiso",
            )
        )

        loop = asyncio.get_running_loop()
        timeout = self._settings.agent.CONFIRMATION_TIMEOUT_SECONDS

        try:
            approved = await asyncio.wait_for(
                loop.run_in_executor(None, self._read_confirmation_choice),
                timeout=timeout,
            )
        except TimeoutError:
            self._console.print(f"  [{_DIM}]Timed out. Operation cancelled.[/]\n")
            return False
        except (EOFError, asyncio.CancelledError):
            return False

        self._console.print(f"  [{_DIM}]Decision:[/] {'permitido' if approved else 'bloqueado'}\n")
        return approved

    def _read_confirmation_choice(self) -> bool:
        """Read a confirmation choice prioritizing single-key interaction."""
        key = self._read_single_key()
        decision = self._interpret_confirmation_key(key)
        if decision is not None:
            return decision

        answer = self._console.input("  Permitir ejecucion? [y/N] \u203a ")
        normalized = answer.strip().lower()
        if not normalized:
            return False
        if normalized.startswith("si"):
            return True
        return normalized in _CONFIRM_ALLOW_KEYS or normalized in {"yes"}

    def _interpret_confirmation_key(self, key: str | None) -> bool | None:
        """Map a single key press to allow/deny/unknown."""
        if key is None:
            return None
        lowered = key.lower()
        if lowered in _CONFIRM_ALLOW_KEYS:
            return True
        if lowered in _CONFIRM_DENY_KEYS:
            return False
        return None

    def _read_single_key(self) -> str | None:
        """Read one key without requiring Enter. Returns None if unsupported."""
        stdin = sys.stdin
        if not hasattr(stdin, "fileno"):
            return None

        try:
            fd = stdin.fileno()
        except (AttributeError, OSError):
            return None

        if not stdin.isatty():
            return None

        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        except (termios.error, OSError):
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

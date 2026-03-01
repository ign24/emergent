"""Tests for terminal channel dashboard UX."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console

from emergent.channels.terminal import TerminalChannel
from emergent.config import EmergentSettings


def _build_channel() -> TerminalChannel:
    settings = EmergentSettings()
    runtime = AsyncMock()
    store = AsyncMock()
    retriever = AsyncMock()
    retriever.upsert_session = AsyncMock()
    context_builder = AsyncMock()
    context_builder._retriever = retriever
    context_builder.build_context = AsyncMock(return_value=(None, None, None, []))
    context_builder.should_summarize = lambda history: False
    return TerminalChannel(
        settings=settings,
        runtime=runtime,
        store=store,
        context_builder=context_builder,
    )


def _render_panel(channel: TerminalChannel, width: int) -> str:
    channel._get_dashboard_width = lambda: width  # type: ignore[method-assign]
    panel = channel._build_live_panel()
    console = Console(record=True, width=max(width, 40))
    console.print(panel)
    return console.export_text()


@pytest.mark.asyncio
async def test_process_message_updates_dashboard_metrics() -> None:
    channel = _build_channel()
    channel._runtime.run = AsyncMock(
        return_value=(
            "ok",
            {
                "total_input_tokens": 11,
                "total_output_tokens": 7,
                "tools_called": ["shell", "web_fetch"],
            },
        )
    )
    channel._console.print = lambda *args, **kwargs: None

    await channel._process_message("hola")

    assert channel._assistant_turns == 1
    assert channel._user_turns == 1
    assert channel._total_tokens == 18
    assert channel._tool_calls == 2


def test_live_panel_renders_key_metrics() -> None:
    channel = _build_channel()
    channel._stats.user_turns = 2
    channel._stats.assistant_turns = 2
    channel._stats.total_tokens = 100
    channel._stats.tool_calls = 3
    channel._stats.latencies_s = [1.0, 2.0]
    channel.set_scheduler_jobs(7)

    rendered = _render_panel(channel, width=140)

    assert "tareas" in rendered
    assert "7" in rendered
    assert "tokens" in rendered
    assert "100" in rendered
    assert "llm" in rendered


def test_live_panel_includes_scheduler_jobs() -> None:
    channel = _build_channel()
    channel.set_scheduler_jobs(7)

    rendered = _render_panel(channel, width=120)
    assert "tareas" in rendered
    assert "7" in rendered


def test_live_panel_includes_voice_status() -> None:
    channel = _build_channel()
    channel.set_voice_status("listening", "escuchando")

    rendered = _render_panel(channel, width=120)

    assert "voz" in rendered
    assert "listening" in rendered


def test_live_panel_includes_assistant_status() -> None:
    channel = _build_channel()
    channel._set_assistant_status("writing", "escribiendo...", animated=False)

    rendered = _render_panel(channel, width=140)

    assert "asist" in rendered
    assert "writing" in rendered


def test_live_panel_animated_assistant_status_uses_professional_indicator() -> None:
    channel = _build_channel()
    channel._supports_live_ui = True
    channel._set_assistant_status("writing", "escribiendo...", animated=True)
    channel._assistant_spinner_index = 2

    rendered = _render_panel(channel, width=140)

    assert "[...]" in rendered
    assert "writing" in rendered
    assert "escribiendo..." in rendered


def test_live_panel_compact_layout_keeps_sections_visible() -> None:
    channel = _build_channel()
    channel.set_voice_status("listening", "estado de voz largo para truncar en compacto")
    channel._set_assistant_status("thinking", "estado largo de asistente", animated=False)
    channel._session_id = "terminal_session_con_nombre_excesivamente_largo"

    rendered = _render_panel(channel, width=72)

    assert "panel en vivo" in rendered
    assert "estado" in rendered
    assert "rendimiento" in rendered
    assert "sesion" in rendered
    assert "lat" in rendered


def test_live_panel_medium_layout_renders_grouped_rows() -> None:
    channel = _build_channel()
    channel._stats.total_tokens = 1234
    channel._stats.tool_calls = 4
    channel._stats.user_turns = 5
    channel._stats.assistant_turns = 4

    rendered = _render_panel(channel, width=100)

    assert "estado" in rendered
    assert "sesion" in rendered
    assert "rendimiento" in rendered
    assert "tokens" in rendered


def test_live_panel_wide_layout_uses_two_rows() -> None:
    channel = _build_channel()
    channel._stats.total_tokens = 999
    channel._stats.tool_calls = 2

    rendered = _render_panel(channel, width=150)

    assert "estado" in rendered
    assert "rendimiento" in rendered
    assert "llm" in rendered


def test_refresh_live_once_skips_updates_while_streaming_response() -> None:
    channel = _build_channel()
    live = Mock()
    channel._live = live

    channel._streaming_response = True
    channel._refresh_live_once()
    live.update.assert_not_called()

    channel._streaming_response = False
    channel._refresh_live_once()
    live.update.assert_called_once()


def test_escape_twice_exits() -> None:
    channel = _build_channel()

    assert channel._register_escape_press("\x1b") is False
    assert channel._register_escape_press("\x1b") is True
    assert channel._register_escape_press("hola") is False


def test_interpret_confirmation_key_accepts_shortcuts() -> None:
    channel = _build_channel()

    assert channel._interpret_confirmation_key("y") is True
    assert channel._interpret_confirmation_key("Y") is True
    assert channel._interpret_confirmation_key("s") is True
    assert channel._interpret_confirmation_key("n") is False
    assert channel._interpret_confirmation_key("\x1b") is False
    assert channel._interpret_confirmation_key("x") is None


def test_read_confirmation_choice_falls_back_to_prompt() -> None:
    channel = _build_channel()
    channel._read_single_key = lambda: None
    channel._console.input = lambda prompt="", **kwargs: "si"  # type: ignore[method-assign]

    assert channel._read_confirmation_choice() is True


@pytest.mark.asyncio
async def test_handle_sigint_signal_cancels_pending_input() -> None:
    channel = _build_channel()
    loop = asyncio.get_running_loop()
    pending: asyncio.Future[str] = loop.create_future()
    channel._input_future = pending
    channel._running = True

    channel._handle_sigint_signal()

    assert channel._running is False
    assert pending.cancelled() is True
    assert channel._interrupted is True


@pytest.mark.asyncio
async def test_local_skill_command_oneshot_executes_wrapped_prompt() -> None:
    channel = _build_channel()
    channel._runtime.run = AsyncMock(
        return_value=("ok", {"total_input_tokens": 0, "total_output_tokens": 0})
    )
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/brainstorming mejorar onboarding")

    assert consumed is True
    await_args = channel._runtime.run.await_args
    assert await_args is not None
    sent = await_args.kwargs["user_message"]
    assert "Skill activo: brainstorming" in sent
    assert "mejorar onboarding" in sent


@pytest.mark.asyncio
async def test_process_message_streams_text_deltas_live() -> None:
    channel = _build_channel()
    printed: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture_print(*args: object, **kwargs: object) -> None:
        printed.append((args, kwargs))

    async def runtime_run(**kwargs: object) -> tuple[str, dict[str, int]]:
        on_text_delta = kwargs.get("on_text_delta")
        assert callable(on_text_delta)
        typed_callback = cast(Callable[[str], Awaitable[None]], on_text_delta)
        await typed_callback("Hola")
        await typed_callback(" mundo")
        return "Hola mundo", {"total_input_tokens": 5, "total_output_tokens": 3}

    channel._console.print = capture_print  # type: ignore[method-assign]
    channel._runtime.run = AsyncMock(side_effect=runtime_run)

    await channel._process_message("hola")

    streamed_chunks = [
        args[0]
        for args, kwargs in printed
        if args and kwargs.get("end") == "" and isinstance(args[0], str)
    ]
    assert "Hola" in streamed_chunks
    assert " mundo" in streamed_chunks


@pytest.mark.asyncio
async def test_local_skill_persistent_mode_changes_active_skill() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/skill debugger")

    assert consumed is True
    assert channel._active_skill == "debugger"


@pytest.mark.asyncio
async def test_local_session_command_updates_session_id() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/session terminal_auto")

    assert consumed is True
    assert channel._session_id == "terminal_auto"


@pytest.mark.asyncio
async def test_panel_command_toggles_dashboard_visibility() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None

    assert channel._dashboard_enabled is False

    on = await channel._handle_local_command("/panel on")
    assert on is True
    assert channel._dashboard_enabled is True

    off = await channel._handle_local_command("/panel off")
    assert off is True
    assert channel._dashboard_enabled is False


@pytest.mark.asyncio
async def test_voice_command_runs_capture_once() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None
    voice = AsyncMock()
    voice.enabled = True
    channel.set_voice_channel(voice)

    consumed = await channel._handle_local_command("/voice 2")

    assert consumed is True
    voice.capture_once.assert_awaited_once_with(duration_seconds=2)


@pytest.mark.asyncio
async def test_voice_command_without_args_starts_continuous_mode() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None
    voice = AsyncMock()
    voice.enabled = True
    channel.set_voice_channel(voice)

    consumed = await channel._handle_local_command("/voice")

    assert consumed is True
    voice.start_continuous.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_voice_off_command_stops_continuous_mode() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None
    voice = AsyncMock()
    voice.enabled = True
    channel.set_voice_channel(voice)

    consumed = await channel._handle_local_command("/voice-off")

    assert consumed is True
    voice.stop_continuous.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_voice_command_when_unavailable_is_consumed() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/voice")

    assert consumed is True


@pytest.mark.asyncio
async def test_local_session_command_lists_and_selects_existing_session() -> None:
    channel = _build_channel()
    channel._store.get_all_sessions_with_names = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"session_id": "terminal_auto", "display_name": "auto-fix", "turns": 8},
            {"session_id": "terminal_debug", "display_name": "debug-crash", "turns": 3},
        ]
    )
    channel._console.print = lambda *args, **kwargs: None
    listed = await channel._handle_local_command("/session")
    consumed = await channel._handle_local_command("/session 2")

    assert listed is True
    assert consumed is True
    assert channel._session_id == "terminal_auto"

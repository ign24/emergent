"""Tests for Telegram channel UX status flow."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from emergent.channels.telegram import TelegramGateway, _format_chunk_for_telegram
from emergent.config import EmergentSettings, TelegramConfig


def _build_gateway() -> TelegramGateway:
    settings = EmergentSettings(
        telegram=TelegramConfig(bot_token="123456:ABCDEF", allowed_user_ids=[1]),
    )
    runtime = AsyncMock()
    store = AsyncMock()
    retriever = AsyncMock()
    retriever.upsert_session = AsyncMock()
    context_builder = AsyncMock()
    context_builder._retriever = retriever
    context_builder.build_context = AsyncMock(return_value=(None, None, None, []))
    context_builder.should_summarize = lambda history: False
    return TelegramGateway(
        settings=settings,
        runtime=runtime,
        store=store,
        context_builder=context_builder,
    )


@pytest.mark.asyncio
async def test_status_update_falls_back_to_new_message_on_edit_error() -> None:
    gateway = _build_gateway()
    gateway._bot = AsyncMock()
    gateway._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
    gateway._bot.edit_message_text = AsyncMock(side_effect=RuntimeError("boom"))

    handle = await gateway._status_start(chat_id=1, text="initial")
    await gateway._status_update(handle, "updated")

    assert gateway._bot.edit_message_text.await_count == 1
    assert gateway._bot.send_message.await_count == 2
    assert handle.message_id == 100


@pytest.mark.asyncio
async def test_process_message_runs_animated_status_until_ready() -> None:
    gateway = _build_gateway()
    gateway._bot = AsyncMock()
    gateway._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    gateway._bot.edit_message_text = AsyncMock(return_value=True)
    gateway._bot.send_chat_action = AsyncMock(return_value=True)
    gateway._send_response = AsyncMock(return_value=None)
    gateway._get_or_create_session = AsyncMock(return_value="s1")

    async def runtime_run(**kwargs: object) -> tuple[str, dict[str, int]]:
        del kwargs
        await asyncio.sleep(0.03)
        return "respuesta final", {}

    gateway._runtime.run = AsyncMock(side_effect=runtime_run)

    async def run_status_animation_fast(status: Any, interval_seconds: float = 1.2) -> None:
        del interval_seconds
        await TelegramGateway._run_status_animation(gateway, status, interval_seconds=0.01)

    gateway._run_status_animation = run_status_animation_fast  # type: ignore[method-assign]

    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        text="hola",
        from_user=SimpleNamespace(id=1, username="nacho"),
    )

    await gateway._process_message(cast(Any, message))

    edit_texts = [call.kwargs["text"] for call in gateway._bot.edit_message_text.await_args_list]
    assert any("Procesando solicitud" in text for text in edit_texts)
    assert len(edit_texts) >= 2
    assert any("Solicitud completada" in text for text in edit_texts)


def test_format_chunk_for_telegram_converts_markdown_like_syntax() -> None:
    chunk = "## **Herramientas**\n- **Bold** `code`"

    formatted = _format_chunk_for_telegram(chunk)

    assert "<b>Herramientas</b>" in formatted
    assert "<b>Bold</b>" in formatted
    assert "<code>code</code>" in formatted


@pytest.mark.asyncio
async def test_session_command_sends_picker_and_skips_runtime() -> None:
    gateway = _build_gateway()
    gateway._bot = AsyncMock()
    runtime_run = AsyncMock(return_value=("ok", {}))
    gateway._runtime.run = runtime_run
    gateway._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=10))
    gateway._get_or_create_session = AsyncMock(return_value="session-current")
    gateway._store.list_chat_sessions_with_names = AsyncMock(
        return_value=[
            {"session_id": "session-old", "display_name": "debug-login"},
            {"session_id": "session-current", "display_name": "session-current"},
        ]
    )

    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        text="/session",
        from_user=SimpleNamespace(id=1, username="nacho"),
    )

    await gateway._process_message(cast(Any, message))

    assert runtime_run.await_count == 0
    assert gateway._bot.send_message.await_count == 1
    sent_kwargs = gateway._bot.send_message.await_args.kwargs
    assert sent_kwargs["chat_id"] == 1
    assert sent_kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_session_pick_callback_updates_active_session() -> None:
    gateway = _build_gateway()
    gateway._store.save_session_mapping = AsyncMock(return_value=None)
    gateway._pending_session_picks["abc12345"] = (1, "session-old")

    callback_message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        edit_reply_markup=AsyncMock(return_value=None),
        answer=AsyncMock(return_value=None),
    )
    callback = SimpleNamespace(
        data="session_pick_abc12345",
        message=callback_message,
        answer=AsyncMock(return_value=None),
    )

    await gateway._handle_confirmation_callback(cast(Any, callback))

    assert gateway._sessions[1] == "session-old"
    gateway._store.save_session_mapping.assert_awaited_once_with(1, "session-old")
    callback.answer.assert_awaited_once()

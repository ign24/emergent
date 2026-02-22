"""Tests for Telegram channel UX status flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
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


def _build_gateway_with_skill_dirs(skill_dirs: list[Path]) -> TelegramGateway:
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
        skill_dirs=skill_dirs,
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
    assert any("88 millas por hora" in text for text in edit_texts)
    assert any("1.21 giggawats" in text or "Circuitos temporales" in text for text in edit_texts)
    assert any("88 mph" in text for text in edit_texts)


def test_format_chunk_for_telegram_converts_markdown_like_syntax() -> None:
    chunk = "## **Herramientas**\n- **Bold** `code`"

    formatted = _format_chunk_for_telegram(chunk)

    assert "<b>Herramientas</b>" in formatted
    assert "<b>Bold</b>" in formatted
    assert "<code>code</code>" in formatted


@pytest.mark.asyncio
async def test_skill_command_sets_persistent_skill_for_chat() -> None:
    gateway = _build_gateway()
    gateway._send_response = AsyncMock(return_value=None)
    message = SimpleNamespace(chat=SimpleNamespace(id=1))

    result = await gateway._maybe_handle_local_command(cast(Any, message), "/skill debugger")

    assert result is None
    assert gateway._active_skill_by_chat[1] == "debugger"
    assert gateway._send_response.await_count >= 1


def test_dynamic_skill_discovery_from_filesystem(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    custom_dir = skills_dir / "my-custom-skill"
    custom_dir.mkdir(parents=True)
    (custom_dir / "SKILL.md").write_text(
        '---\nname: my-custom-skill\ndescription: "Skill personalizada de prueba."\n---\n',
        encoding="utf-8",
    )

    gateway = _build_gateway_with_skill_dirs([skills_dir])

    assert "my-custom-skill" in gateway._available_skills
    assert (
        gateway._available_skills["my-custom-skill"].description == "Skill personalizada de prueba."
    )

"""Tests for terminal channel dashboard UX."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

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


def _build_channel_with_skill_dirs(skill_dirs: list[Path]) -> TerminalChannel:
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
        skill_dirs=skill_dirs,
    )


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

    panel = channel._build_live_panel()
    console = Console(record=True, width=120)
    console.print(panel)
    rendered = console.export_text()

    assert "jobs" in rendered
    assert "7" in rendered
    assert "tokens" in rendered
    assert "100" in rendered
    assert "llm" in rendered
    assert "AUTO" in rendered


def test_live_panel_includes_scheduler_jobs() -> None:
    channel = _build_channel()
    channel.set_scheduler_jobs(7)

    panel = channel._build_live_panel()
    console = Console(record=True, width=120)
    console.print(panel)
    rendered = console.export_text()
    assert "jobs" in rendered
    assert "7" in rendered


def test_escape_twice_exits() -> None:
    channel = _build_channel()

    assert channel._register_escape_press("\x1b") is False
    assert channel._register_escape_press("\x1b") is True
    assert channel._register_escape_press("hola") is False


@pytest.mark.asyncio
async def test_local_skill_command_oneshot_executes_wrapped_prompt() -> None:
    channel = _build_channel()
    channel._runtime.run = AsyncMock(
        return_value=("ok", {"total_input_tokens": 0, "total_output_tokens": 0})
    )
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/brainstorming mejorar onboarding")

    assert consumed is True
    assert channel._runtime.run.await_args is not None
    sent = channel._runtime.run.await_args.kwargs["user_message"]
    assert "Skill activo: brainstorming" in sent
    assert "mejorar onboarding" in sent


@pytest.mark.asyncio
async def test_local_skill_persistent_mode_changes_active_skill() -> None:
    channel = _build_channel()
    channel._console.print = lambda *args, **kwargs: None

    consumed = await channel._handle_local_command("/skill debugger")

    assert consumed is True
    assert channel._active_skill == "debugger"


def test_dynamic_skill_discovery_from_filesystem(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    custom_dir = skills_dir / "my-custom-skill"
    custom_dir.mkdir(parents=True)
    (custom_dir / "SKILL.md").write_text(
        '---\nname: my-custom-skill\ndescription: "Skill personalizada de prueba."\n---\n',
        encoding="utf-8",
    )

    channel = _build_channel_with_skill_dirs([skills_dir])

    assert "my-custom-skill" in channel._available_skills
    assert (
        channel._available_skills["my-custom-skill"].description == "Skill personalizada de prueba."
    )

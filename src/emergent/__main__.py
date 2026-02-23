"""Entrypoint — python -m emergent."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog
from apscheduler.triggers.cron import CronTrigger

logger = structlog.get_logger(__name__)


async def _run() -> None:
    from emergent.config import get_settings, verify_guards_integrity
    from emergent.observability.tracing import configure_logging

    settings = get_settings()
    verify_guards_integrity(settings)

    # Initialize data directory (needed before configure_logging for log_file path)
    mem_cfg = settings.memory or {}
    data_dir = Path(settings.agent.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / mem_cfg.get("sqlite_db", "emergent.db")
    chroma_dir = data_dir / mem_cfg.get("chroma_dir", "chroma")
    chroma_dir.mkdir(parents=True, exist_ok=True)

    # Default log_file: data/logs/emergent.log (overridable via config)
    obs_cfg = settings.observability or {}
    log_file = obs_cfg.get("log_file") or str(data_dir / "logs" / "emergent.log")

    # Configure structlog — file gets all logs, terminal only WARNING+
    configure_logging(
        log_level=obs_cfg.get("log_level", "INFO"),
        log_format="json",
        log_file=log_file,
    )

    log = structlog.get_logger(__name__)
    log.info("emergent_starting", version="0.1.0", model=settings.agent.model)

    # Initialize components
    from emergent.agent.context import ContextBuilder
    from emergent.agent.runtime import AgentRuntime
    from emergent.channels.telegram import TelegramGateway
    from emergent.memory.retriever import SemanticRetriever
    from emergent.memory.store import MemoryStore
    from emergent.research.worker import ResearchWorker
    from emergent.tools import ExecutionContext, create_registry
    from emergent.tools.cron import TOOL_DEFINITION as CRON_TOOL_DEF
    from emergent.tools.cron import cron_schedule, init_scheduler
    from emergent.tools.memory_tools import (
        MEMORY_SEARCH_DEFINITION,
        MEMORY_STORE_DEFINITION,
        make_memory_search_handler,
        make_memory_store_handler,
    )
    from emergent.tools.registry import SafetyTier, ToolDefinition
    from emergent.tools.research import (
        RESEARCH_RUN_DEFINITION,
        RESEARCH_SEARCH_DEFINITION,
        make_research_run_handler,
        make_research_search_handler,
    )

    store = MemoryStore(db_path)
    retriever = SemanticRetriever(chroma_dir)
    context_builder = ContextBuilder(
        store=store,
        retriever=retriever,
        context_budget_tokens=mem_cfg.get("context_budget_tokens", 20000),
        summarize_at_pct=mem_cfg.get("summarize_at_pct", 0.80),
        max_history_turns=mem_cfg.get("max_history_turns", 12),
        history_keep_after_summary=mem_cfg.get("history_keep_after_summary", 4),
    )

    # Build tool registry
    registry = create_registry(execution_context=ExecutionContext.USER_SESSION)

    # Add memory tools
    registry.register(
        ToolDefinition(
            name="memory_search",
            description=MEMORY_SEARCH_DEFINITION["description"],
            input_schema=MEMORY_SEARCH_DEFINITION["input_schema"],
            handler=make_memory_search_handler(retriever),
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )
    registry.register(
        ToolDefinition(
            name="memory_store",
            description=MEMORY_STORE_DEFINITION["description"],
            input_schema=MEMORY_STORE_DEFINITION["input_schema"],
            handler=make_memory_store_handler(store),
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )

    # Agent runtime (created before cron so callback can reference it)
    runtime = AgentRuntime(
        settings=settings,
        registry=registry,
    )

    # Console notifier for terminal activity lines
    from emergent.observability.banner import ConsoleNotifier

    notifier = ConsoleNotifier()

    # Terminal channel (always created)
    from emergent.channels.terminal import TerminalChannel

    terminal = TerminalChannel(
        settings=settings,
        runtime=runtime,
        store=store,
        context_builder=context_builder,
    )

    # Telegram gateway (only if token is configured)
    telegram_enabled = bool(settings.telegram.bot_token)
    gateway: TelegramGateway | None = None
    if telegram_enabled:
        gateway = TelegramGateway(
            settings=settings,
            runtime=runtime,
            store=store,
            context_builder=context_builder,
            notifier=notifier,
        )

    # Optional research worker reference used by cron callback.
    research_worker: ResearchWorker | None = None

    # Voice channel (optional, local-first push-to-talk)
    voice_cfg = settings.voice or {}
    voice_enabled = bool(voice_cfg.get("enabled", False))
    voice_channel = None
    terminal.set_voice_status("off", "desactivado")
    if voice_enabled:
        try:
            from emergent.channels.voice import VoiceChannel

            voice_channel = VoiceChannel(
                settings=settings,
                runtime=runtime,
                store=store,
                context_builder=context_builder,
                status_callback=terminal.set_voice_status,
            )
            terminal.set_voice_channel(voice_channel)
            terminal.set_voice_status("starting", "canal de voz iniciando")
        except Exception as e:
            log.error("voice_channel_init_failed", error=str(e))
            voice_enabled = False
            terminal.set_voice_status("error", "no se pudo inicializar")

    # --- Cron callback: runs a prompt headlessly and notifies via Telegram ---
    async def _cron_run_callback(prompt: str) -> str:
        log.info("cron_callback_invoked", prompt=prompt[:60])
        prompt_lower = prompt.lower()
        research_triggers = (
            "research:",
            "investigacion",
            "investigación",
            "investigar",
            "research job",
        )
        should_run_research = any(trigger in prompt_lower for trigger in research_triggers)

        if should_run_research and research_worker is not None:
            topic = prompt
            if prompt_lower.startswith("research:"):
                topic = prompt.split(":", 1)[1].strip() or "emergent improvements"
            try:
                highlights, rest = await research_worker.run_adhoc(
                    topic=topic,
                    max_results=settings.research.max_findings_per_domain,
                )
                response_text = (
                    "[cron] Research job completado. "
                    f"highlights={len(highlights)} rest={len(rest)} topic='{topic[:80]}'"
                )
            except Exception as e:
                log.error("cron_callback_research_error", error=str(e), topic=topic[:80])
                response_text = f"[cron] Error al ejecutar research job: {e}"
        else:
            try:
                response_text, _ = await runtime.run(
                    user_message=prompt,
                    session_id="cron_headless",
                )
            except Exception as e:
                log.error("cron_callback_runtime_error", error=str(e))
                response_text = f"[cron] Error al ejecutar: {e}"

        # Notify all allowed users via Telegram (only if gateway exists)
        if gateway is not None:
            for chat_id in settings.telegram.allowed_user_ids:
                try:
                    await gateway._bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ *[cron]* `{prompt[:50]}`\n\n{response_text}",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    log.error("cron_telegram_notify_failed", chat_id=chat_id, error=str(e))

        return response_text

    # Add cron tool (wired with callback)
    async def _cron_handler(tool_input: dict) -> str:
        return await cron_schedule(tool_input)

    registry.register(
        ToolDefinition(
            name="cron_schedule",
            description=CRON_TOOL_DEF["description"],
            input_schema=CRON_TOOL_DEF["input_schema"],
            handler=_cron_handler,
            safety_tier=SafetyTier.TIER_2_CONFIRM,
        )
    )

    # Initialize scheduler with SQLite persistence and the Telegram callback
    db_url = f"sqlite:///{db_path.resolve()}"
    scheduler = init_scheduler(db_url=db_url, run_callback=_cron_run_callback)

    # Register maintenance jobs (replace_existing=True so they survive re-registration)
    scheduler.add_job(
        store.cleanup_old_data,
        trigger="cron",
        hour=3,
        minute=0,
        id="maintenance_cleanup",
        name="cleanup_old_data",
        replace_existing=True,
        jobstore="volatile",
    )
    scheduler.add_job(
        store.decay_profile_confidence,
        trigger="cron",
        day=1,
        hour=4,
        minute=0,
        id="maintenance_decay",
        name="decay_profile_confidence",
        replace_existing=True,
        jobstore="volatile",
    )

    # Research worker + tool wiring
    research_cfg = settings.research
    if research_cfg.enabled:
        research_worker = ResearchWorker(
            store=store,
            retriever=retriever,
            settings=settings,
            telegram_notify=gateway,
        )
        try:
            trigger = CronTrigger.from_crontab(research_cfg.schedule)
            scheduler.add_job(
                research_worker.run,
                trigger=trigger,
                id="research_daily",
                name="daily_research",
                replace_existing=True,
                jobstore="volatile",
            )
        except Exception as e:
            log.error("research_schedule_invalid", schedule=research_cfg.schedule, error=str(e))

        registry.register(
            ToolDefinition(
                name="research_search",
                description=RESEARCH_SEARCH_DEFINITION["description"],
                input_schema=RESEARCH_SEARCH_DEFINITION["input_schema"],
                handler=make_research_search_handler(store, retriever),
                safety_tier=SafetyTier.TIER_1_AUTO,
            )
        )
        registry.register(
            ToolDefinition(
                name="research_run",
                description=RESEARCH_RUN_DEFINITION["description"],
                input_schema=RESEARCH_RUN_DEFINITION["input_schema"],
                handler=make_research_run_handler(research_worker),
                safety_tier=SafetyTier.TIER_2_CONFIRM,
            )
        )

    scheduler.start()
    terminal.set_scheduler_jobs(len(scheduler.get_jobs()))
    log.info("scheduler_started", persistent_db=db_url)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    channel_tasks: list[asyncio.Task[None]] = []

    def _handle_sigterm() -> None:
        log.info("sigterm_received")
        for task in channel_tasks:
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    log.info("emergent_ready", allowed_users=len(settings.telegram.allowed_user_ids))

    from emergent.observability.banner import print_banner

    print_banner(
        version="0.1.0",
        provider="AUTO" if settings.agent.routing_enabled else settings.agent.provider,
        model=settings.agent.model,
        db_path=str(db_path),
        chroma_dir=str(chroma_dir),
        allowed_users=len(settings.telegram.allowed_user_ids),
        scheduler_jobs=len(scheduler.get_jobs()),
        log_file=log_file,
        telegram_enabled=telegram_enabled,
        voice_enabled=voice_enabled,
    )

    if voice_channel is not None:
        await voice_channel.start()

    try:
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(terminal.start(), name="terminal"),
        ]
        if gateway is not None:
            tasks.append(asyncio.create_task(gateway.start(), name="telegram"))
        channel_tasks = tasks

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # When terminal exits, stop everything else
        for t in pending:
            t.cancel()
        # Propagate exceptions from completed tasks
        for t in done:
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                log.error("channel_error", task=t.get_name(), error=str(t.exception()))
    finally:
        await terminal.stop()
        if gateway is not None:
            await gateway.stop()
        if voice_channel is not None:
            await voice_channel.stop()
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await store.close()
        log.info("emergent_stopped")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nEmergent stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()

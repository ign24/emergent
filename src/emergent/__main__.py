"""Entrypoint — python -m emergent."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog

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

    store = MemoryStore(db_path)
    retriever = SemanticRetriever(chroma_dir)
    context_builder = ContextBuilder(
        store=store,
        retriever=retriever,
        context_budget_tokens=mem_cfg.get("context_budget_tokens", 20000),
        summarize_at_pct=mem_cfg.get("summarize_at_pct", 0.80),
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

    # Telegram gateway
    gateway = TelegramGateway(
        settings=settings,
        runtime=runtime,
        store=store,
        context_builder=context_builder,
        notifier=notifier,
    )

    # --- Cron callback: runs a prompt headlessly and notifies via Telegram ---
    async def _cron_run_callback(prompt: str) -> str:
        log.info("cron_callback_invoked", prompt=prompt[:60])
        try:
            response_text, _ = await runtime.run(
                user_message=prompt,
                session_id="cron_headless",
            )
        except Exception as e:
            log.error("cron_callback_runtime_error", error=str(e))
            response_text = f"[cron] Error al ejecutar: {e}"

        # Notify all allowed users via Telegram
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
    )
    scheduler.start()
    log.info("scheduler_started", persistent_db=db_url)

    # Graceful shutdown
    loop = asyncio.get_running_loop()

    def _handle_sigterm() -> None:
        log.info("sigterm_received")
        asyncio.create_task(gateway.stop())

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    log.info("emergent_ready", allowed_users=len(settings.telegram.allowed_user_ids))

    from emergent.observability.banner import print_banner
    print_banner(
        version="0.1.0",
        model=settings.agent.model,
        db_path=str(db_path),
        chroma_dir=str(chroma_dir),
        allowed_users=len(settings.telegram.allowed_user_ids),
        scheduler_jobs=len(scheduler.get_jobs()),
        log_file=log_file,
    )

    try:
        await gateway.start()
    finally:
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

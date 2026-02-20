"""Cron scheduling tool using APScheduler with SQLite persistence."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from emergent import SafetyViolationError

logger = structlog.get_logger(__name__)

MIN_INTERVAL_MINUTES = 5

# Module-level singletons
_scheduler: AsyncIOScheduler | None = None
_run_callback: Callable[[str], Awaitable[str]] | None = None


def init_scheduler(
    db_url: str,
    run_callback: Callable[[str], Awaitable[str]] | None = None,
) -> AsyncIOScheduler:
    """
    Initialize the scheduler with SQLite persistence. Call once at startup.

    Args:
        db_url: SQLAlchemy URL, e.g. 'sqlite:////abs/path/to/emergent.db'
        run_callback: async function(prompt) -> str called when a job fires.
    """
    global _scheduler, _run_callback
    _run_callback = run_callback

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    _scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=db_url)},
    )
    logger.info("scheduler_initialized", db_url=db_url, has_callback=run_callback is not None)
    return _scheduler


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        # Fallback: in-memory scheduler (no persistence) — for tests or headless use
        _scheduler = AsyncIOScheduler()
    return _scheduler


async def _run_prompt_job(prompt: str) -> None:
    """Top-level job function (picklable). Calls the module-level run_callback."""
    logger.info("cron_job_fired", prompt=prompt[:60])
    cb = _run_callback
    if cb is None:
        logger.warning("cron_job_no_callback", prompt=prompt[:60])
        return
    try:
        result = await cb(prompt)
        logger.info("cron_job_done", result_len=len(result))
    except Exception as e:
        logger.error("cron_job_failed", error=str(e), prompt=prompt[:60])


async def cron_schedule(
    tool_input: dict[str, Any],
    run_agent_callback: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Create, list, or delete scheduled jobs."""
    action = str(tool_input.get("action", "list"))

    if action == "list":
        return _list_jobs()
    elif action == "create":
        return await _create_job(tool_input)
    elif action == "delete":
        return _delete_job(tool_input)
    else:
        return f"Error: unknown action '{action}'. Use 'create', 'list', or 'delete'."


def _list_jobs() -> str:
    scheduler = get_scheduler()
    jobs = scheduler.get_jobs()
    if not jobs:
        return "No hay jobs programados."

    lines = ["Jobs programados:"]
    for job in jobs:
        lines.append(f"  - {job.id}: {job.name} | próxima: {job.next_run_time}")
    return "\n".join(lines)


async def _create_job(tool_input: dict[str, Any]) -> str:
    cron_expr = str(tool_input.get("cron_expression", ""))
    prompt = str(tool_input.get("prompt", "")).strip()
    job_id = str(tool_input.get("job_id", str(uuid.uuid4())[:8]))

    if not cron_expr:
        return "Error: cron_expression is required"

    if not prompt:
        return "Error: prompt is required"

    if len(prompt) > 500:
        return "Error: prompt exceeds 500 characters"

    # Check for write/destructive intent in prompt
    _BLOCKED_INTENT = ["rm ", "kill ", "sudo ", "delete ", "remove ", "format ", "drop "]
    prompt_lower = prompt.lower()
    for blocked in _BLOCKED_INTENT:
        if blocked in prompt_lower:
            raise SafetyViolationError(
                "CRON_PROMPT_BLOCKED: cron prompts cannot contain write/destructive intent"
            )

    try:
        trigger = CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        return f"Error: invalid cron expression '{cron_expr}': {e}"

    scheduler = get_scheduler()
    scheduler.add_job(
        _run_prompt_job,
        trigger=trigger,
        id=job_id,
        name=f"emergent:{prompt[:30]}",
        replace_existing=True,
        kwargs={"prompt": prompt},
    )

    if not scheduler.running:
        scheduler.start()

    persistence = "con persistencia SQLite" if hasattr(scheduler, "_jobstores") and "default" in scheduler._jobstores else "en memoria"
    logger.info("cron_job_created", job_id=job_id, cron=cron_expr, prompt=prompt[:50])
    return f"Job '{job_id}' creado ({persistence}) con cron '{cron_expr}'."


def _delete_job(tool_input: dict[str, Any]) -> str:
    job_id = str(tool_input.get("job_id", ""))
    if not job_id:
        return "Error: job_id is required"

    scheduler = get_scheduler()
    job = scheduler.get_job(job_id)
    if not job:
        return f"Error: job '{job_id}' not found"

    scheduler.remove_job(job_id)
    logger.info("cron_job_deleted", job_id=job_id)
    return f"Job '{job_id}' eliminado."


TOOL_DEFINITION = {
    "name": "cron_schedule",
    "description": (
        "Create, list, or delete scheduled cron jobs. "
        "Jobs run the agent with a predefined prompt at the scheduled time. "
        "Cron prompts must be read-only in intent (no destructive actions). "
        "Minimum interval: every 5 minutes. "
        "Actions: 'create' (TIER_2, needs confirmation), 'list' (TIER_1), 'delete' (TIER_2)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "delete"],
                "description": "Action to perform",
            },
            "job_id": {
                "type": "string",
                "description": "Job identifier (for create/delete). Auto-generated if not provided.",
            },
            "cron_expression": {
                "type": "string",
                "description": "Standard cron expression (e.g., '*/15 * * * *' for every 15min)",
            },
            "prompt": {
                "type": "string",
                "description": "The read-only prompt to run at schedule time. Max 500 chars.",
                "maxLength": 500,
            },
        },
        "required": ["action"],
    },
}

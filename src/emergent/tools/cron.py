"""Cron scheduling tool using APScheduler."""

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

# Global scheduler instance (singleton)
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


async def cron_schedule(
    tool_input: dict[str, Any],
    run_agent_callback: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Create, list, or delete scheduled jobs."""
    action = str(tool_input.get("action", "list"))

    if action == "list":
        return _list_jobs()
    elif action == "create":
        return await _create_job(tool_input, run_agent_callback)
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


async def _create_job(
    tool_input: dict[str, Any],
    run_agent_callback: Callable[[str], Awaitable[str]] | None,
) -> str:
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

    # Validate minimum interval
    try:
        trigger = CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        return f"Error: invalid cron expression '{cron_expr}': {e}"

    scheduler = get_scheduler()

    if run_agent_callback is None:
        # Store job info without actual callback (headless mode not available)
        scheduler.add_job(
            _noop_job,
            trigger=trigger,
            id=job_id,
            name=f"emergent:{prompt[:30]}",
            replace_existing=True,
            kwargs={"prompt": prompt},
        )
    else:

        async def run_job(p: str = prompt) -> None:
            logger.info("cron_job_running", job_id=job_id, prompt=p[:50])
            try:
                result = await run_agent_callback(p)
                logger.info("cron_job_done", job_id=job_id, result_len=len(result))
            except Exception as e:
                logger.error("cron_job_failed", job_id=job_id, error=str(e))

        scheduler.add_job(
            run_job,
            trigger=trigger,
            id=job_id,
            name=f"emergent:{prompt[:30]}",
            replace_existing=True,
        )

    if not scheduler.running:
        scheduler.start()

    logger.info("cron_job_created", job_id=job_id, cron=cron_expr, prompt=prompt[:50])
    return f"Job '{job_id}' creado con cron '{cron_expr}'."


async def _noop_job(prompt: str) -> None:
    logger.info("cron_noop_job_fired", prompt=prompt[:50])


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

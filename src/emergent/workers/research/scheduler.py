"""Scheduler integration for research worker jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler


def register_research_jobs(
    *,
    scheduler: AsyncIOScheduler,
    weekly_callback: Callable[[], Awaitable[None]],
    pulse_callback: Callable[[], Awaitable[None]] | None = None,
    weekly_job_id: str = "research_weekly",
    pulse_job_id: str = "research_pulse",
) -> None:
    """Register weekly and optional pulse jobs."""
    scheduler.add_job(
        weekly_callback,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=30,
        id=weekly_job_id,
        name="research_weekly",
        replace_existing=True,
    )

    if pulse_callback is not None:
        scheduler.add_job(
            pulse_callback,
            trigger="cron",
            hour=9,
            minute=30,
            id=pulse_job_id,
            name="research_pulse",
            replace_existing=True,
        )

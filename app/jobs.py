"""Background jobs — thin wrappers around the SQL functions in migrations/005.

The job *bodies* live in Postgres (`core.sweep_inactive_leads`,
`events.relay_domain_events`) so that one implementation serves two runners:

  * Supabase: pg_cron calls them directly (hourly / every 2 min). The API
    container can sleep; the funnel keeps ticking. docs/DECISIONS.md §14.
  * Local compose (plain postgres:17-alpine, no pg_cron): this module's
    APScheduler calls the same functions on the FastAPI lifespan.

ENABLE_SCHEDULER gates the in-process runner. On a Supabase deploy leave it
false — pg_cron owns the jobs there, and running both double-runs the sweep
(config.py logs a warning if you do). Still not multi-worker safe: one process.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import get_settings
from app.db import get_sessionmaker
from app.services import events

log = logging.getLogger(__name__)


async def relay_events() -> int:
    """Scheduled wrapper around events.relay_events with its own session."""
    async with get_sessionmaker()() as session:
        return await events.relay_events(session)


async def sweep_inactive_leads() -> int:
    """Raise a follow-up task + a NOTE on every lead that has gone quiet.

    Delegates to core.sweep_inactive_leads(p_hours). Idempotent by design: a
    lead that already has a PENDING task is skipped, so running the sweep twice
    in an hour does not produce two tasks.
    """
    settings = get_settings()
    async with get_sessionmaker()() as session:
        raised = (
            await session.execute(
                text("select core.sweep_inactive_leads(:hours)"),
                {"hours": settings.inactivity_hours},
            )
        ).scalar_one()
        await session.commit()

    if raised:
        log.info("inactivity sweep: raised follow-ups for %d lead(s)", raised)
    return raised


def build_scheduler() -> AsyncIOScheduler | None:
    settings = get_settings()
    if not settings.enable_scheduler:
        log.info("scheduler disabled (ENABLE_SCHEDULER=false)")
        return None

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        sweep_inactive_leads,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval_minutes),
        id="inactivity_sweep",
        name="Raise follow-ups for leads with no outbound contact",
        max_instances=1,
        coalesce=True,          # a missed run does not pile up
        misfire_grace_time=300,
    )
    scheduler.add_job(
        relay_events,
        trigger=IntervalTrigger(seconds=settings.event_relay_seconds),
        id="event_relay",
        name="Publish unpublished domain events from the outbox",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    return scheduler

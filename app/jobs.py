"""Inactivity sweep.

CLAUDE.md cut the event bus: inactivity detection is a scheduled job, not an
event consumer. APScheduler runs it in-process on the FastAPI lifespan.

Gated by ENABLE_SCHEDULER because it is NOT distributed-safe: every replica that
runs it would raise its own duplicate task for the same lead. Run it on exactly
one worker, or move it to pg_cron. (Recorded in docs/DECISIONS.md.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, or_, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Agent, FollowUpTask, Interaction, Lead, LeadStage

log = logging.getLogger(__name__)


async def sweep_inactive_leads() -> int:
    """Raise a follow-up task + a NOTE on every lead that has gone quiet.

    Idempotent by design: a lead that already has a PENDING task is skipped, so
    running the sweep twice in an hour does not produce two tasks.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.inactivity_hours)

    async with get_sessionmaker()() as session:
        terminal = select(LeadStage.code).where(LeadStage.is_terminal.is_(True))

        last_outbound = (
            select(func.max(Interaction.occurred_at))
            .where(Interaction.lead_id == Lead.id, Interaction.direction == "OUTBOUND")
            .correlate(Lead)
            .scalar_subquery()
        )
        already_queued = (
            select(FollowUpTask.id)
            .where(FollowUpTask.lead_id == Lead.id, FollowUpTask.status == "PENDING")
            .correlate(Lead)
            .exists()
        )

        stale = (
            await session.execute(
                select(Lead)
                .where(
                    Lead.current_stage.not_in(terminal),
                    Lead.created_at < cutoff,
                    or_(last_outbound.is_(None), last_outbound < cutoff),
                    ~already_queued,
                )
                .limit(500)
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for lead in stale:
            session.add(
                FollowUpTask(
                    lead_id=lead.id,
                    agent_id=lead.agent_id,
                    due_at=now + timedelta(hours=24),
                    note=(
                        f"Auto-raised: no outbound contact in "
                        f"{settings.inactivity_hours}h."
                    ),
                    status="PENDING",
                )
            )
            session.add(
                Interaction(
                    lead_id=lead.id,
                    direction="OUTBOUND",
                    channel="IN_APP",
                    type="NOTE",
                    body=(
                        f"Lead flagged inactive after {settings.inactivity_hours}h "
                        f"with no outbound contact."
                    ),
                    occurred_at=now,
                    created_by=lead.agent_id,
                )
            )

        await session.commit()

    if stale:
        log.info("inactivity sweep: raised follow-ups for %d lead(s)", len(stale))
    return len(stale)


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
    return scheduler

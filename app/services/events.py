"""Domain-event outbox.

CLAUDE.md cut Kafka: events are recorded synchronously in the request that
triggers them. `emit` does exactly that — it adds the outbox row to the
**caller's** session, with no commit of its own, so the event lands iff the
business transaction commits.

Publishing is the job of `events.relay_domain_events()` in Postgres
(migrations/005): pg_cron runs it every 2 min on Supabase, and `jobs.relay_events`
runs it from the in-process scheduler on the local container. `relay_events`
below is the Python entry point to that same function.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DomainEvent

log = logging.getLogger(__name__)

RELAY_BATCH = 100


def emit(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    agency_id: uuid.UUID,
    payload: dict | None = None,
) -> DomainEvent:
    """Stage an outbox row in the caller's transaction. Does not commit."""
    event = DomainEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        agency_id=agency_id,
        payload=payload or {},
    )
    session.add(event)
    return event


async def relay_events(session: AsyncSession, batch: int = RELAY_BATCH) -> int:
    """Publish up to `batch` unpublished events, oldest first. Returns the count.

    Delegates to events.relay_domain_events(): runs the per-type handler
    (today: `lead.created` -> first-touch follow-up) and stamps published_at,
    under `for update skip locked` so two runners never dispatch the same row.
    """
    published = (
        await session.execute(
            text("select events.relay_domain_events(:batch)"), {"batch": batch}
        )
    ).scalar_one()
    await session.commit()
    if published:
        log.info("relay: published %d event(s)", published)
    return published

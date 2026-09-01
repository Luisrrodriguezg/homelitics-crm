"""Domain-event outbox.

CLAUDE.md cut Kafka: events are recorded synchronously in the request that
triggers them. `emit` does exactly that — it adds the outbox row to the
**caller's** session, with no commit of its own, so the event lands iff the
business transaction commits. A slow in-process relay (`jobs.relay_events`)
then dispatches unpublished rows to handlers and stamps `published_at`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
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


# ------------------------------------------------------------ relay

# event_type -> async handler(session, event). Registered by jobs.py to avoid
# an import cycle (handlers touch lead/task logic).
HANDLERS: dict[str, "callable"] = {}


def register(event_type: str):
    def _wrap(fn):
        HANDLERS[event_type] = fn
        return fn
    return _wrap


async def relay_events(session: AsyncSession) -> int:
    """Dispatch unpublished events, oldest first. Returns the count published.

    A handler that raises leaves the row unpublished with `attempts` bumped, so
    the next tick retries it; one bad event does not stall the rest.
    """
    rows = (
        await session.execute(
            select(DomainEvent)
            .where(DomainEvent.published_at.is_(None))
            .order_by(DomainEvent.occurred_at)
            .limit(RELAY_BATCH)
        )
    ).scalars().all()

    published = 0
    for event in rows:
        handler = HANDLERS.get(event.event_type)
        try:
            if handler is not None:
                await handler(session, event)
            event.published_at = datetime.now(timezone.utc)
            published += 1
        except Exception:  # noqa: BLE001 — isolate one bad event
            event.attempts += 1
            log.exception("event %s (%s) failed, will retry", event.id, event.event_type)
        await session.flush()

    await session.commit()
    if published:
        log.info("relay: published %d event(s)", published)
    return published

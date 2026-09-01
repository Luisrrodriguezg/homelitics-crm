"""The domain-event outbox: written in the caller's transaction, published once.

`emit` adds the row to the caller's session with no commit of its own, so the
event exists iff the business fact commits. `jobs.relay_events` then publishes
unpublished rows exactly once.
"""
import uuid

import pytest
from sqlalchemy import select

import app.jobs  # registers the event handlers
from app.models import DomainEvent, FollowUpTask
from app.services import events


async def _events_for(session, agency_id, event_type=None):
    q = select(DomainEvent).where(DomainEvent.agency_id == agency_id)
    if event_type:
        q = q.where(DomainEvent.event_type == event_type)
    return list((await session.execute(q)).scalars().all())


async def test_creating_a_lead_writes_one_lead_created_event(world, client_for, session):
    agency_id = world.agents[0][0].agency_id
    c = client_for(world.agents[0][0])
    r = await c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
    })
    assert r.status_code == 201, r.text

    rows = await _events_for(session, agency_id, "lead.created")
    assert len(rows) == 1
    assert rows[0].aggregate_id == uuid.UUID(r.json()["id"])
    assert rows[0].published_at is None


async def test_emit_is_bound_to_the_callers_transaction(world, session):
    agency_id = world.agents[0][0].agency_id
    events.emit(
        session, event_type="test.rollback", aggregate_type="lead",
        aggregate_id=uuid.uuid4(), agency_id=agency_id, payload={},
    )
    await session.rollback()
    assert await _events_for(session, agency_id, "test.rollback") == []


async def test_relay_publishes_once_and_runs_the_handler(world, client_for, session):
    agency_id = world.agents[0][0].agency_id
    c = client_for(world.agents[0][0])
    lead_id = (await c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
    })).json()["id"]

    first = await events.relay_events(session)
    assert first >= 1

    row = (await session.execute(
        select(DomainEvent).where(
            DomainEvent.agency_id == agency_id,
            DomainEvent.event_type == "lead.created",
        )
    )).scalar_one()
    assert row.published_at is not None

    # on_lead_created raised the first-touch follow-up
    tasks = (await session.execute(
        select(FollowUpTask).where(FollowUpTask.lead_id == uuid.UUID(lead_id))
    )).scalars().all()
    assert len(tasks) == 1

    # second pass is a no-op for this event
    before = await _events_for(session, agency_id)
    unpublished = [e for e in before if e.published_at is None
                   and e.event_type == "lead.created"]
    assert unpublished == []

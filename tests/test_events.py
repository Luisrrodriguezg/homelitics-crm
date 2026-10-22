"""The domain-event outbox: written in the caller's transaction, published once.

`emit` adds the row to the caller's session with no commit of its own, so the
event exists iff the business fact commits. `jobs.relay_events` then publishes
unpublished rows exactly once.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

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
        "source_channel": "TELEGRAM",
    })
    assert r.status_code == 201, r.text

    rows = await _events_for(session, agency_id, "lead.created")
    assert len(rows) == 1
    assert rows[0].aggregate_id == uuid.UUID(r.json()["id"])
    assert rows[0].published_at is None


async def test_events_record_who_acted(world, client_for, session):
    """013: the bot's lead and the human's transition each carry their actor."""
    agency_id = world.agents[0][0].agency_id
    bot, human = world.bots[0], world.agents[0][0]
    r = await client_for(bot).post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code == 201, r.text
    moved = await client_for(human).post(f"/leads/{r.json()['id']}/transitions",
                                         json={"to_stage": "LOST", "lost_reason": "PRICE"})
    assert moved.status_code in (200, 201), moved.text

    created = await _events_for(session, agency_id, "lead.created")
    changed = await _events_for(session, agency_id, "lead.stage_changed")
    assert [e.actor_agent_id for e in created] == [bot.id]
    assert [e.actor_agent_id for e in changed] == [human.id]


def _backfill_statements() -> list[str]:
    """The UPDATEs of 013, read from the migration itself so the test cannot
    drift from what is applied to the live database."""
    sql = (Path(__file__).parent.parent / "migrations" / "013_event_actor.sql").read_text()
    chunks = ("\n".join(l for l in c.splitlines() if not l.lstrip().startswith("--")).strip()
              for c in sql.split(";"))
    return [c for c in chunks if c.lower().startswith("update")]


async def test_013_backfill_recovers_the_actor(world, client_for, session):
    agency_id = world.agents[0][0].agency_id
    owner = world.agents[0][0]
    c = client_for(owner)
    lead = (await c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })).json()["id"]
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
        hour=15, minute=0, second=0, microsecond=0)
    r = await c.post(f"/leads/{lead}/appointments",
                     json={"scheduled_at": when.isoformat(), "duration_min": 60})
    assert r.status_code == 201, r.text

    # Pretend these events predate 013, then run its backfill.
    await session.execute(update(DomainEvent).where(DomainEvent.agency_id == agency_id)
                          .values(actor_agent_id=None))
    statements = _backfill_statements()
    assert len(statements) == 2
    for stmt in statements:
        await session.execute(text(stmt))
    await session.commit()

    rows = await _events_for(session, agency_id)
    kinds = {e.event_type for e in rows}
    assert {"lead.created", "lead.stage_changed", "appointment.booked"} <= kinds, kinds
    assert all(e.actor_agent_id == owner.id for e in rows), \
        [(e.event_type, e.actor_agent_id) for e in rows]


async def test_emit_is_bound_to_the_callers_transaction(world, session):
    agency_id = world.agents[0][0].agency_id
    events.emit(
        session, event_type="test.rollback", aggregate_type="lead",
        aggregate_id=uuid.uuid4(), agency_id=agency_id, actor_id=None, payload={},
    )
    await session.rollback()
    assert await _events_for(session, agency_id, "test.rollback") == []


async def test_relay_publishes_once_and_runs_the_handler(world, client_for, session):
    agency_id = world.agents[0][0].agency_id
    c = client_for(world.agents[0][0])
    lead_id = (await c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
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

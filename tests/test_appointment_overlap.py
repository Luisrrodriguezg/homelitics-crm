"""The EXCLUDE constraint was cut from the schema, so overlap prevention is the
API's job. If this breaks, agents get double-booked and nothing complains.

Overlap is about the *agent's* calendar, so these book different leads of the
same agent: a lead holds at most one open visit (009), and a second post on the
same lead is the idempotent-retry path, tested at the bottom.
"""
from datetime import datetime, timedelta, timezone

import asyncio


async def _lead(c, world, client_id):
    r = await c.post("/leads", json={
        "client_id": str(client_id),
        "listing_id": str(world.listings[0].id),     # one listing -> one agent
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _new_client(c, world, name):
    # Named with the fixture tag so the world teardown sweeps it.
    r = await c.post("/clients", json={"full_name": f"{world.tag} {name}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_overlapping_visit_is_rejected(world, client_for):
    c = client_for(world.agents[0][0])
    first_lead = await _lead(c, world, world.clients[0].id)
    second_lead = await _lead(c, world, world.clients[1].id)
    start = datetime.now(timezone.utc) + timedelta(days=3)

    first = await c.post(f"/leads/{first_lead}/appointments",
                         json={"scheduled_at": start.isoformat(), "duration_min": 60})
    assert first.status_code == 201, first.text

    # another client, starting 30 min into the first visit
    clash = await c.post(f"/leads/{second_lead}/appointments",
                         json={"scheduled_at": (start + timedelta(minutes=30)).isoformat(),
                               "duration_min": 60})
    assert clash.status_code == 409, clash.text
    assert "overlap" in clash.json()["detail"].lower()


async def test_back_to_back_is_allowed(world, client_for):
    """Half-open intervals: an appointment ending exactly when the next begins
    is not an overlap. Getting this wrong makes the calendar unusable."""
    c = client_for(world.agents[0][0])
    first_lead = await _lead(c, world, world.clients[0].id)
    second_lead = await _lead(c, world, world.clients[1].id)
    start = datetime.now(timezone.utc) + timedelta(days=5)

    a = await c.post(f"/leads/{first_lead}/appointments",
                     json={"scheduled_at": start.isoformat(), "duration_min": 60})
    b = await c.post(f"/leads/{second_lead}/appointments",
                     json={"scheduled_at": (start + timedelta(minutes=60)).isoformat(),
                           "duration_min": 60})
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text


async def test_concurrent_bookings_only_one_wins(world, client_for):
    """The lock is the point. Without it four clients asking for the same slot
    all read 'no overlap' and all insert."""
    c = client_for(world.agents[0][0])
    clients = [world.clients[0].id, world.clients[1].id,
               await _new_client(c, world, "client 2"), await _new_client(c, world, "client 3")]
    leads = [await _lead(c, world, cid) for cid in clients]
    start = (datetime.now(timezone.utc) + timedelta(days=9)).replace(microsecond=0)
    payload = {"scheduled_at": start.isoformat(), "duration_min": 60}

    results = await asyncio.gather(
        *[c.post(f"/leads/{lead}/appointments", json=payload) for lead in leads])
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409], [r.text for r in results]


async def test_retrying_the_same_booking_is_idempotent(world, client_for):
    """A bot that retries after a timeout must get its visit back, not a 409
    from its own booking — and never a second visit."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world, world.clients[0].id)
    start = (datetime.now(timezone.utc) + timedelta(days=11)).replace(microsecond=0)
    payload = {"scheduled_at": start.isoformat(), "duration_min": 60}

    results = await asyncio.gather(
        *[c.post(f"/leads/{lead}/appointments", json=payload) for _ in range(4)])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200, 200, 201], [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1

    visits = (await c.get(f"/leads/{lead}/appointments")).json()
    assert len(visits) == 1


async def test_past_visit_rejected(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world, world.clients[0].id)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = await c.post(f"/leads/{lead}/appointments", json={"scheduled_at": past})
    assert r.status_code == 422

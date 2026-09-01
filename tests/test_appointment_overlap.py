"""The EXCLUDE constraint was cut from the schema, so overlap prevention is the
API's job. If this breaks, agents get double-booked and nothing complains."""
from datetime import datetime, timedelta, timezone

import asyncio
import pytest


async def _lead(c, world, client_idx=0):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_overlapping_visit_is_rejected(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    start = datetime.now(timezone.utc) + timedelta(days=3)

    first = await c.post(f"/leads/{lead}/appointments",
                   json={"scheduled_at": start.isoformat(), "duration_min": 60})
    assert first.status_code == 201, first.text

    # starts 30 min into the first one
    clash = await c.post(f"/leads/{lead}/appointments",
                   json={"scheduled_at": (start + timedelta(minutes=30)).isoformat(),
                         "duration_min": 60})
    assert clash.status_code == 409, clash.text
    assert "overlap" in clash.json()["detail"].lower()


async def test_back_to_back_is_allowed(world, client_for):
    """Half-open intervals: an appointment ending exactly when the next begins
    is not an overlap. Getting this wrong makes the calendar unusable."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    start = datetime.now(timezone.utc) + timedelta(days=5)

    a = await c.post(f"/leads/{lead}/appointments",
               json={"scheduled_at": start.isoformat(), "duration_min": 60})
    b = await c.post(f"/leads/{lead}/appointments",
               json={"scheduled_at": (start + timedelta(minutes=60)).isoformat(),
                     "duration_min": 60})
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text


async def test_concurrent_bookings_only_one_wins(world, client_for):
    """The row lock is the point. Without SELECT ... FOR UPDATE both requests
    read 'no overlap' and both insert."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    start = (datetime.now(timezone.utc) + timedelta(days=9)).replace(microsecond=0)
    payload = {"scheduled_at": start.isoformat(), "duration_min": 60}

    results = await asyncio.gather(
        *[c.post(f"/leads/{lead}/appointments", json=payload) for _ in range(4)])
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, f"expected exactly one booking to win, got {codes}"
    assert all(x in (201, 409) for x in codes), codes


async def test_past_visit_rejected(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = await c.post(f"/leads/{lead}/appointments", json={"scheduled_at": past})
    assert r.status_code == 422

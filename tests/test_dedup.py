"""HU-01 CA3: posting the same (client, listing) twice must not duplicate.

This is the test that would silently pass with a Python-side check and fail in
production, so it deliberately fires the two requests concurrently.
"""
import asyncio

import pytest


async def test_same_pair_returns_existing_thread(world, client_for):
    agent = world.agents[0][0]
    c = client_for(agent)
    body = {
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
        "message": "Interested",
    }

    first = c.post("/leads", json=body)
    assert first.status_code == 201, first.text

    second = c.post("/leads", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]


async def test_concurrent_posts_produce_one_lead(world, client_for):
    """Two simultaneous requests: one 201, one 200, one row. Never two rows,
    never a 500 from the UNIQUE violation."""
    agent = world.agents[0][0]
    c = client_for(agent)
    body = {
        "client_id": str(world.clients[1].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "IN_APP",
    }

    results = await asyncio.gather(
        *[asyncio.to_thread(c.post, "/leads", json=body) for _ in range(6)]
    )
    codes = sorted(r.status_code for r in results)
    ids = {r.json()["id"] for r in results}

    assert all(code in (200, 201) for code in codes), codes
    assert codes.count(201) == 1, f"expected exactly one creator, got {codes}"
    assert len(ids) == 1, f"dedup failed — {len(ids)} distinct leads created"


async def test_opening_transition_and_stage_cache(world, client_for):
    """The trigger must set current_stage from the opening transition."""
    agent = world.agents[0][0]
    c = client_for(agent)
    r = c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "CALL",
    })
    lead_id = r.json()["id"]
    assert r.json()["current_stage"] == "INTERESTED"

    hist = c.get(f"/leads/{lead_id}/transitions").json()
    assert [t["to_stage"] for t in hist] == ["INTERESTED"]
    assert hist[0]["from_stage"] is None

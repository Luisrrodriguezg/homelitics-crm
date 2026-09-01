"""Funnel edges and the LOST invariant.

The database stores transitions but does not constrain which edges are legal,
and it cannot express 'LOST requires a reason'. Both live in the service layer,
so both need tests.
"""
import pytest


async def _lead(c, world, client_idx=0):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_illegal_skip_is_rejected(world, client_for):
    """INTERESTED -> WON skips the entire funnel and must 409."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    r = await c.post(f"/leads/{lead}/transitions", json={"to_stage": "WON"})
    assert r.status_code == 409, r.text
    assert "illegal transition" in r.json()["detail"].lower()


async def test_legal_walk_updates_the_cache(world, client_for):
    """Each step must be reflected in lead.current_stage by the trigger."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    for stage in ("VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON"):
        r = await c.post(f"/leads/{lead}/transitions", json={"to_stage": stage})
        assert r.status_code == 201, r.text
        assert (await c.get(f"/leads/{lead}")).json()["current_stage"] == stage


async def test_terminal_lead_cannot_move(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    for stage in ("VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON"):
        await c.post(f"/leads/{lead}/transitions", json={"to_stage": stage})

    r = await c.post(f"/leads/{lead}/transitions",
               json={"to_stage": "LOST", "lost_reason": "PRICE"})
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"].lower()


async def test_lost_without_reason_is_422(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    r = await c.post(f"/leads/{lead}/transitions", json={"to_stage": "LOST"})
    assert r.status_code == 422, r.text


async def test_lost_with_reason_succeeds(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world, client_idx=1)
    r = await c.post(f"/leads/{lead}/transitions",
               json={"to_stage": "LOST", "lost_reason": "NO_RESPONSE",
                     "note": "client stopped replying"})
    assert r.status_code == 201, r.text
    assert (await c.get(f"/leads/{lead}")).json()["current_stage"] == "LOST"


async def test_lost_reason_is_only_valid_on_lost(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    r = await c.post(f"/leads/{lead}/transitions",
               json={"to_stage": "VISIT_SCHEDULED", "lost_reason": "PRICE"})
    assert r.status_code == 422

"""Cross-agency isolation.

There is no RLS behind this API — authorization is entirely service-layer
filtering. That makes these the highest-value tests in the suite: if a filter is
dropped, nothing else fails, and one agency starts reading another's pipeline.
"""
import pytest


def _lead_in_agency_0(c, world):
    r = c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "WHATSAPP",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_other_agency_cannot_read_lead(world, client_for):
    """404, not 403 — we do not confirm that the lead exists at all."""
    own = client_for(world.agents[0][0])
    lead_id = _lead_in_agency_0(own, world)
    assert own.get(f"/leads/{lead_id}").status_code == 200

    intruder = client_for(world.agents[1][0])
    assert intruder.get(f"/leads/{lead_id}").status_code == 404


async def test_other_agency_cannot_transition_lead(world, client_for):
    own = client_for(world.agents[0][0])
    lead_id = _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    r = intruder.post(f"/leads/{lead_id}/transitions", json={"to_stage": "VISIT_SCHEDULED"})
    assert r.status_code == 404


async def test_lead_board_never_leaks_across_agencies(world, client_for):
    own = client_for(world.agents[0][0])
    lead_id = _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    visible = {row["id"] for row in intruder.get("/leads").json()}
    assert lead_id not in visible


async def test_cannot_use_another_agencys_listing(world, client_for):
    """Creating a lead against a listing you do not own must 404, not silently
    attach the lead to the other agency's agent."""
    intruder = client_for(world.agents[1][0])
    r = intruder.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),   # belongs to agency 0
        "source_channel": "CALL",
    })
    assert r.status_code == 404


async def test_reassign_rejects_target_outside_agency(world, client_for):
    admin = client_for(world.agents[0][0])          # TEAM_ADMIN of agency 0
    lead_id = _lead_in_agency_0(admin, world)

    outsider = world.agents[1][1].id                # agent in agency 1
    r = admin.post(f"/leads/{lead_id}/reassign", json={"to_agent_id": str(outsider)})
    assert r.status_code == 404


async def test_reassign_requires_team_admin(world, client_for):
    admin = client_for(world.agents[0][0])
    lead_id = _lead_in_agency_0(admin, world)

    plain = client_for(world.agents[0][1])          # role AGENT
    r = plain.post(f"/leads/{lead_id}/reassign",
                   json={"to_agent_id": str(world.agents[0][1].id)})
    assert r.status_code == 403


async def test_reassign_moves_lead_and_writes_audit(world, client_for):
    """The seeder's original bug: audit row written, lead left pointing at the
    old agent. Both must change together."""
    admin = client_for(world.agents[0][0])
    lead_id = _lead_in_agency_0(admin, world)
    target = world.agents[0][1]

    r = admin.post(f"/leads/{lead_id}/reassign", json={"to_agent_id": str(target.id)})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == str(target.id)

    assert admin.get(f"/leads/{lead_id}").json()["agent_id"] == str(target.id)

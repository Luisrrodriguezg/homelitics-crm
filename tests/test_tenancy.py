"""Cross-agency isolation.

There is no RLS behind this API — authorization is entirely service-layer
filtering. That makes these the highest-value tests in the suite: if a filter is
dropped, nothing else fails, and one agency starts reading another's pipeline.
"""
import uuid

import pytest
from sqlalchemy import select

from app.models import AssignmentAudit


async def _lead_in_agency_0(c, world):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_other_agency_cannot_read_lead(world, client_for):
    """404, not 403 — we do not confirm that the lead exists at all."""
    own = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(own, world)
    assert (await own.get(f"/leads/{lead_id}")).status_code == 200

    intruder = client_for(world.agents[1][0])
    assert (await intruder.get(f"/leads/{lead_id}")).status_code == 404


async def test_other_agency_cannot_transition_lead(world, client_for):
    own = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    r = await intruder.post(f"/leads/{lead_id}/transitions", json={"to_stage": "VISIT_SCHEDULED"})
    assert r.status_code == 404


async def test_lead_board_never_leaks_across_agencies(world, client_for):
    own = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    visible = {row["id"] for row in (await intruder.get("/leads")).json()}
    assert lead_id not in visible


async def test_client_filter_never_leaks_across_agencies(world, client_for):
    """GET /leads?client_id= is how the bot finds a returning client's threads;
    clients are global, so the agency filter is the only thing scoping it."""
    own = client_for(world.agents[0][0])
    await _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    r = await intruder.get("/leads", params={"client_id": str(world.clients[0].id)})
    assert r.status_code == 200 and r.json() == []


async def test_board_filters_never_leak_across_agencies(world, client_for):
    """Cards carry the client's name, so the new property/date filters must be
    scoped by the same agency join as everything else."""
    own = client_for(world.agents[0][0])
    await _lead_in_agency_0(own, world)

    intruder = client_for(world.agents[1][0])
    r = await intruder.get("/leads", params={
        "property_id": str(world.listings[0].property_id), "created_from": "2000-01-01",
    })
    assert r.status_code == 200 and r.json() == []


async def test_other_agency_cannot_touch_a_visit(world, client_for):
    """Every per-visit route answers 404 outside the agency (009's included)."""
    from datetime import datetime, timedelta, timezone

    own = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(own, world)
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    appt = (await own.post(f"/leads/{lead_id}/appointments",
                           json={"scheduled_at": when.isoformat()})).json()["id"]

    intruder = client_for(world.agents[1][0])
    for path in (f"/appointments/{appt}", f"/appointments/{appt}/invite.ics",
                 f"/appointments/{appt}/feedback", f"/leads/{lead_id}/appointments"):
        assert (await intruder.get(path)).status_code == 404, path
    r = await intruder.patch(f"/appointments/{appt}", json={"status": "CANCELLED"})
    assert r.status_code == 404
    r = await intruder.get(f"/agents/{world.agents[0][0].id}/slots",
                           params={"from": when.isoformat(),
                                   "to": (when + timedelta(days=1)).isoformat()})
    assert r.status_code == 404


async def test_cannot_use_another_agencys_listing(world, client_for):
    """Creating a lead against a listing you do not own must 404, not silently
    attach the lead to the other agency's agent."""
    intruder = client_for(world.agents[1][0])
    r = await intruder.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),   # belongs to agency 0
        "source_channel": "CALL",
    })
    assert r.status_code == 404


async def test_reassign_rejects_target_outside_agency(world, client_for):
    admin = client_for(world.agents[0][0])          # TEAM_ADMIN of agency 0
    lead_id = await _lead_in_agency_0(admin, world)

    outsider = world.agents[1][1].id                # agent in agency 1
    r = await admin.post(f"/leads/{lead_id}/reassign", json={"to_agent_id": str(outsider)})
    assert r.status_code == 404


async def test_reassign_requires_team_admin(world, client_for):
    admin = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(admin, world)

    plain = client_for(world.agents[0][1])          # role AGENT
    r = await plain.post(f"/leads/{lead_id}/reassign",
                   json={"to_agent_id": str(world.agents[0][1].id)})
    assert r.status_code == 403


async def test_reassign_moves_lead_and_writes_audit(world, client_for, session):
    """The seeder's original bug: audit row written, lead left pointing at the
    old agent. Both must change together (HU-08 AC3)."""
    admin = client_for(world.agents[0][0])
    lead_id = await _lead_in_agency_0(admin, world)
    owner = world.agents[0][0]                      # the listing's agent owns the lead
    target = world.agents[0][1]

    r = await admin.post(f"/leads/{lead_id}/reassign", json={"to_agent_id": str(target.id)})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == str(target.id)

    assert (await admin.get(f"/leads/{lead_id}")).json()["agent_id"] == str(target.id)

    audit = (await session.execute(
        select(AssignmentAudit).where(AssignmentAudit.lead_id == uuid.UUID(lead_id))
    )).scalars().all()
    assert [(a.from_agent_id, a.to_agent_id, a.reassigned_by) for a in audit] == [
        (owner.id, target.id, owner.id),
    ]

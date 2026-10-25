"""The in-app inbox (014, docs/DECISIONS.md §23).

A message is written only when the AI agent or a TEAM_ADMIN hands an agent work:
a lead assigned to them, or a visit booked on their calendar. In the fixture
`world.agents[a][0]` is agency a's TEAM_ADMIN and owns its listing,
`world.agents[a][1]` is a plain AGENT, `world.bots[a]` is the AI agent.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.conftest import get_new_session
from tests.test_visit_rules import BOGOTA, _at, _book, _lead, _open_hours


async def _inbox(c, **params):
    r = await c.get("/me/notifications", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _bot_lead(world, client_for, client_idx=0):
    return await _lead(client_for(world.bots[0]), world, client_idx)


# ------------------------------------------------------------- who gets one

async def test_bot_created_lead_lands_in_the_owners_inbox(world, client_for):
    owner = world.agents[0][0]
    lead = await _bot_lead(world, client_for)

    page = await _inbox(client_for(owner))
    assert page["unread_count"] == 1
    [n] = page["items"]
    assert n["type"] == "LEAD_ASSIGNED"
    assert n["assigned_via"] == "CREATED"
    assert n["lead_id"] == lead
    assert n["actor_agent_id"] == str(world.bots[0].id)
    assert n["actor_role"] == "AI_AGENT"
    assert n["client_name"].endswith("client 0")
    assert n["listing_address"] == "1 Test St"
    assert n["read"] is False and n["read_at"] is None


async def test_an_agents_own_actions_notify_no_one(world, client_for):
    admin, agent = world.agents[0][0], world.agents[0][1]
    # The admin opens a lead on their own listing; an ordinary agent opens one
    # on the admin's listing and books a visit on it.
    await _lead(client_for(admin), world, 0)
    lead = await _lead(client_for(agent), world, 1)
    assert (await _book(client_for(agent), lead, _at(3, 11))).status_code == 201

    assert (await _inbox(client_for(admin)))["items"] == []
    assert (await _inbox(client_for(agent)))["items"] == []


async def test_reassignment_by_an_admin_notifies_the_new_owner(world, client_for):
    admin, agent = world.agents[0][0], world.agents[0][1]
    lead = await _lead(client_for(admin), world)

    r = await client_for(admin).post(f"/leads/{lead}/reassign",
                                     json={"to_agent_id": str(agent.id)})
    assert r.status_code == 200, r.text

    [n] = (await _inbox(client_for(agent)))["items"]
    assert (n["type"], n["assigned_via"]) == ("LEAD_ASSIGNED", "REASSIGNED")
    assert n["from_agent_id"] == str(admin.id)
    assert n["actor_agent_id"] == str(admin.id)
    assert n["actor_role"] == "TEAM_ADMIN"
    # The admin did it, so nothing for them.
    assert (await _inbox(client_for(admin)))["items"] == []


async def test_reassignment_is_an_outbox_event(world, client_for, session):
    from sqlalchemy import select
    from app.models import DomainEvent
    admin, agent = world.agents[0][0], world.agents[0][1]
    lead = await _lead(client_for(admin), world)
    await client_for(admin).post(f"/leads/{lead}/reassign", json={"to_agent_id": str(agent.id)})

    ev = (await session.execute(
        select(DomainEvent).where(DomainEvent.event_type == "lead.reassigned",
                                  DomainEvent.agency_id == world.agencies[0].id)
    )).scalar_one()
    assert ev.actor_agent_id == admin.id
    assert ev.payload == {"agent_id": str(agent.id), "from_agent_id": str(admin.id)}


async def test_bot_booking_notifies_the_owner_and_shows_the_live_status(world, client_for):
    owner = world.agents[0][0]
    await _open_hours(owner.id)
    lead = await _bot_lead(world, client_for)
    when = _at(3, 10)
    r = await _book(client_for(world.bots[0]), lead, when)
    assert r.status_code == 201, r.text
    visit = r.json()["id"]

    page = await _inbox(client_for(owner), type="VISIT_SCHEDULED")
    [n] = page["items"]
    assert n["appointment_id"] == visit
    assert n["actor_role"] == "AI_AGENT"
    assert n["booked_status"] == n["visit_status"] == "PENDING_CONFIRMATION"
    assert datetime.fromisoformat(n["visit_scheduled_at"]) == when
    assert n["visit_duration_min"] == 60

    # Confirming changes the visit, not the message: visit_status follows it.
    r = await client_for(owner).patch(f"/appointments/{visit}", json={"status": "CONFIRMED"})
    assert r.status_code == 200, r.text
    [n] = (await _inbox(client_for(owner), type="VISIT_SCHEDULED"))["items"]
    assert (n["booked_status"], n["visit_status"]) == ("PENDING_CONFIRMATION", "CONFIRMED")


async def test_admin_booking_on_an_agents_lead_notifies_that_agent(world, client_for):
    admin, agent = world.agents[0][0], world.agents[0][1]
    lead = await _lead(client_for(admin), world)
    await client_for(admin).post(f"/leads/{lead}/reassign", json={"to_agent_id": str(agent.id)})

    assert (await _book(client_for(admin), lead, _at(4, 9))).status_code == 201
    types = [n["type"] for n in (await _inbox(client_for(agent)))["items"]]
    assert types == ["VISIT_SCHEDULED", "LEAD_ASSIGNED"]          # newest first


# ------------------------------------------------------------- read state

async def test_mark_read_is_per_message_and_only_for_the_recipient(world, client_for):
    owner, other = world.agents[0][0], world.agents[0][1]
    await _bot_lead(world, client_for, 0)
    await _bot_lead(world, client_for, 1)
    first, second = (await _inbox(client_for(owner)))["items"]

    assert (await client_for(other).post(f"/me/notifications/{first['id']}/read")).status_code == 404
    assert (await client_for(owner).post(f"/me/notifications/{first['id']}/read")).status_code == 204

    page = await _inbox(client_for(owner))
    assert page["unread_count"] == 1
    assert [n["read"] for n in page["items"]] == [True, False]
    read_at = page["items"][0]["read_at"]

    # Repeating keeps the first read_at.
    await client_for(owner).post(f"/me/notifications/{first['id']}/read")
    assert (await _inbox(client_for(owner)))["items"][0]["read_at"] == read_at

    assert [n["id"] for n in (await _inbox(client_for(owner), read="false"))["items"]] == [second["id"]]
    assert [n["id"] for n in (await _inbox(client_for(owner), read="true"))["items"]] == [first["id"]]


async def test_read_all(world, client_for):
    owner = world.agents[0][0]
    await _bot_lead(world, client_for, 0)
    await _bot_lead(world, client_for, 1)

    r = await client_for(owner).post("/me/notifications/read-all")
    assert r.status_code == 200 and r.json() == {"marked": 2}
    page = await _inbox(client_for(owner))
    assert page["unread_count"] == 0 and all(n["read"] for n in page["items"])
    assert (await client_for(owner).post("/me/notifications/read-all")).json() == {"marked": 0}


async def test_a_bot_cannot_mark_messages_read(world, client_for):
    r = await client_for(world.bots[0]).post("/me/notifications/read-all")
    assert r.status_code == 403


# ------------------------------------------------------------- the monitor

async def test_monitor_is_admin_only_and_agency_scoped(world, client_for):
    await _bot_lead(world, client_for)

    assert (await client_for(world.agents[0][1]).get("/notifications")).status_code == 403

    page = (await client_for(world.agents[0][0]).get("/notifications")).json()
    assert len(page["items"]) == 1
    # Agency 1's admin sees nothing of agency 0.
    other = (await client_for(world.agents[1][0]).get("/notifications")).json()
    assert other["items"] == [] and other["unread_count"] == 0


async def test_monitor_filters_and_never_marks_read(world, client_for):
    admin, agent = world.agents[0][0], world.agents[0][1]
    mon = client_for(admin)
    bot_lead = await _bot_lead(world, client_for, 0)               # bot -> admin
    lead = await _lead(mon, world, 1)
    await mon.post(f"/leads/{lead}/reassign", json={"to_agent_id": str(agent.id)})  # admin -> agent

    async def ids(**params):
        r = await mon.get("/notifications", params=params)
        assert r.status_code == 200, r.text
        return [n["lead_id"] for n in r.json()["items"]]

    assert await ids() == [lead, bot_lead]
    assert await ids(by_bot="true") == [bot_lead]
    assert await ids(by_bot="false") == [lead]
    assert await ids(actor_agent_id=str(world.bots[0].id)) == [bot_lead]
    assert await ids(recipient_agent_id=str(agent.id)) == [lead]
    assert await ids(lead_id=bot_lead) == [bot_lead]
    assert await ids(type="VISIT_SCHEDULED") == []
    today = datetime.now(BOGOTA).date()
    assert len(await ids(created_from=today.isoformat(), created_to=today.isoformat())) == 2
    assert await ids(created_from=(today + timedelta(days=1)).isoformat()) == []
    r = await mon.get("/notifications", params={"created_from": today.isoformat(),
                                                "created_to": (today - timedelta(days=1)).isoformat()})
    assert r.status_code == 422

    # Admin read their own message; the agent has not read theirs.
    mine = (await _inbox(mon))["items"][0]
    await mon.post(f"/me/notifications/{mine['id']}/read")
    assert await ids(read="false") == [lead]
    assert await ids(read="true") == [bot_lead]

    # Looking at the monitor changed nothing for the agent.
    assert (await _inbox(client_for(agent)))["unread_count"] == 1


# ------------------------------------------------------------- paging

async def test_twenty_per_page_newest_first(world, client_for):
    from app.models import Notification
    owner = world.agents[0][0]
    lead = await _bot_lead(world, client_for)                      # message 1
    async with get_new_session() as s:                             # messages 2..25
        s.add_all([
            Notification(type="LEAD_ASSIGNED", agency_id=world.agencies[0].id,
                         recipient_agent_id=owner.id, actor_agent_id=world.bots[0].id,
                         lead_id=lead, data={"via": "CREATED"})
            for _ in range(24)
        ])
        await s.commit()

    first = await _inbox(client_for(owner))
    assert len(first["items"]) == 20 and first["unread_count"] == 25
    ids = [n["id"] for n in first["items"]]
    assert ids == sorted(ids, reverse=True)

    rest = await _inbox(client_for(owner), before=first["next_before"])
    assert len(rest["items"]) == 5 and rest["next_before"] is None
    assert max(n["id"] for n in rest["items"]) < min(ids)

    assert (await client_for(owner).get("/me/notifications", params={"limit": 21})).status_code == 422

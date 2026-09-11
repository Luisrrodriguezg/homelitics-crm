"""The rules around a visit (009, docs/DECISIONS.md §19).

Status is the owning agent's consent; AI agents book only published slots;
one open visit per lead; the calendar drives the funnel; time off never covers
a booked visit; one feedback per side. The bot is `world.bots[0]`, acting in
agency 0, whose listing belongs to `world.agents[0][0]`.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text, update

from tests.conftest import get_new_session

BOGOTA = ZoneInfo("America/Bogota")


def _at(days: int, hh: int, mm: int = 0) -> datetime:
    """A local (Bogotá) wall-clock time `days` from today, in UTC."""
    day = datetime.now(BOGOTA).date() + timedelta(days=days)
    return datetime.combine(day, time(hh, mm), tzinfo=BOGOTA).astimezone(timezone.utc)


async def _open_hours(agent_id, start=time(8), end=time(18)):
    """Publish every day start..end, so an AI agent has something to book."""
    from app.models import AgentAvailability
    async with get_new_session() as s:
        s.add_all([
            AgentAvailability(agent_id=agent_id, weekday=wd, start_time=start, end_time=end,
                              valid_from=date.today() - timedelta(days=1))
            for wd in range(7)
        ])
        await s.commit()


async def _lead(c, world, client_idx=0):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _book(c, lead, when, minutes=60):
    return await c.post(f"/leads/{lead}/appointments",
                        json={"scheduled_at": when.isoformat(), "duration_min": minutes})


async def _stage(c, lead):
    return (await c.get(f"/leads/{lead}")).json()["current_stage"]


async def _grant(world, scope):
    from app.models import ServiceAccount
    async with get_new_session() as s:
        await s.execute(
            update(ServiceAccount).where(ServiceAccount.id == world.service_account_id)
            .values(scopes=world.service_account.scopes + [scope])
        )
        await s.commit()


# ------------------------------------------------------------- consent

async def test_owner_booking_is_confirmed_and_schedules_the_lead(world, client_for):
    owner = world.agents[0][0]
    c = client_for(owner)
    lead = await _lead(c, world)

    r = await _book(c, lead, _at(3, 10))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "CONFIRMED"
    assert r.json()["created_by"] == str(owner.id)

    assert await _stage(c, lead) == "VISIT_SCHEDULED"
    moves = (await c.get(f"/leads/{lead}/transitions")).json()
    assert [(t["from_stage"], t["to_stage"], t["changed_by"]) for t in moves][-1] == \
        ("INTERESTED", "VISIT_SCHEDULED", str(owner.id))


async def test_colleague_booking_waits_for_the_owner(world, client_for):
    colleague, owner = client_for(world.agents[0][1]), client_for(world.agents[0][0])
    lead = await _lead(colleague, world)

    r = await _book(colleague, lead, _at(3, 11))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PENDING_CONFIRMATION"
    assert await _stage(owner, lead) == "INTERESTED"     # nothing agreed yet

    r = await owner.patch(f"/appointments/{r.json()['id']}", json={"status": "CONFIRMED"})
    assert r.status_code == 200, r.text
    assert await _stage(owner, lead) == "VISIT_SCHEDULED"


async def test_bot_booking_is_pending_and_confined_to_published_slots(world, client_for):
    await _open_hours(world.agents[0][0].id)
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world)

    r = await _book(bot, lead, datetime.now(timezone.utc) + timedelta(minutes=30))
    assert r.status_code == 422 and "ahead" in r.json()["detail"]      # too soon

    r = await _book(bot, lead, _at(3, 21))                              # after hours
    assert r.status_code == 409 and "published availability" in r.json()["detail"]

    r = await _book(bot, lead, _at(3, 10))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PENDING_CONFIRMATION"
    assert r.json()["created_by"] == str(world.bots[0].id)
    assert await _stage(bot, lead) == "INTERESTED"

    # the booking is on the lead's timeline, attributed to the bot, and is not
    # an agent response
    lines = (await bot.get(f"/leads/{lead}/interactions")).json()
    assert any(i["type"] == "STATUS_CHANGE" and i["created_by"] == str(world.bots[0].id)
               for i in lines)
    async with get_new_session() as s:
        has_outbound = await s.scalar(
            text("select has_outbound from analytics.lead_outcome where lead_id = :id"),
            {"id": lead},
        )
    assert has_outbound is False


async def test_bot_with_visits_manage_books_confirmed(world, client_for):
    """The instant-booking switch: one grant, no deploy."""
    await _open_hours(world.agents[0][0].id)
    await _grant(world, "visits:manage")
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world)

    r = await _book(bot, lead, _at(4, 9))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "CONFIRMED"
    assert await _stage(bot, lead) == "VISIT_SCHEDULED"


# ------------------------------------------------------ one open visit per lead

async def test_one_open_visit_per_lead(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)

    first = await _book(c, lead, _at(5, 10))
    assert first.status_code == 201, first.text
    again = await _book(c, lead, _at(5, 10))
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    other = await _book(c, lead, _at(6, 10))
    assert other.status_code == 409 and "open visit" in other.json()["detail"]


# ------------------------------------------------------ calendar -> funnel

async def test_completing_a_visit_moves_the_lead_to_visited(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    appt = (await _book(c, lead, _at(3, 15))).json()

    r = await c.patch(f"/appointments/{appt['id']}", json={"status": "COMPLETED"})
    assert r.status_code == 200, r.text
    assert await _stage(c, lead) == "VISITED"


async def test_closing_a_lead_cancels_its_open_visit(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    other = await _lead(c, world, client_idx=1)
    when = _at(3, 16)
    appt = (await _book(c, lead, when)).json()

    r = await c.post(f"/leads/{lead}/transitions",
                     json={"to_stage": "LOST", "lost_reason": "BOUGHT_ELSEWHERE"})
    assert r.status_code == 201, r.text
    assert (await c.get(f"/appointments/{appt['id']}")).json()["status"] == "CANCELLED"

    # the slot is free again, and the closed lead takes no new visit
    assert (await _book(c, other, when)).status_code == 201
    r = await _book(c, lead, _at(7, 10))
    assert r.status_code == 409 and "closed lead" in r.json()["detail"]


# ------------------------------------------------------ what a bot may PATCH

async def test_bot_can_move_and_cancel_but_not_confirm_or_complete(world, client_for):
    await _open_hours(world.agents[0][0].id)
    owner, bot = client_for(world.agents[0][0]), client_for(world.bots[0])
    lead = await _lead(owner, world)
    appt = (await _book(owner, lead, _at(3, 10))).json()
    url = f"/appointments/{appt['id']}"

    # moving within its own slot is not an overlap with itself
    r = await bot.patch(url, json={"scheduled_at": _at(3, 10, 30).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RESCHEDULED"          # the owner re-confirms

    r = await bot.patch(url, json={"scheduled_at": _at(3, 22).isoformat()})
    assert r.status_code == 409                          # outside published hours

    r = await bot.patch(url, json={"status": "CONFIRMED"})
    assert r.status_code == 403 and "visits:manage" in r.json()["detail"]
    r = await bot.patch(url, json={"status": "COMPLETED"})
    assert r.status_code == 403

    r = await bot.patch(url, json={"status": "CANCELLED"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"

    async with get_new_session() as s:
        kinds = (await s.execute(
            text("select event_type from events.domain_event where aggregate_id = :id order by id"),
            {"id": appt["id"]},
        )).scalars().all()
    assert kinds == ["appointment.booked", "appointment.rescheduled", "appointment.cancelled"]


# ------------------------------------------------------ the agent's schedule

async def test_time_off_over_a_booked_visit_is_refused(world, client_for):
    owner = world.agents[0][0]
    c = client_for(owner)
    lead = await _lead(c, world)
    appt = (await _book(c, lead, _at(3, 10))).json()

    r = await c.post(f"/agents/{owner.id}/time-off", json={
        "starts_at": _at(3, 9).isoformat(), "ends_at": _at(3, 12).isoformat(),
        "reason": "dentist",
    })
    assert r.status_code == 409 and appt["id"] in r.json()["detail"]

    r = await c.post(f"/agents/{owner.id}/time-off", json={
        "starts_at": _at(3, 11).isoformat(), "ends_at": _at(3, 12).isoformat(),
    })
    assert r.status_code == 201 and r.json()["source"] == "MANUAL"   # touching is fine


# ------------------------------------------------------ feedback

async def test_client_feedback_through_the_bot(world, client_for):
    owner, bot = client_for(world.agents[0][0]), client_for(world.bots[0])
    lead = await _lead(owner, world)
    appt = (await _book(owner, lead, _at(3, 13))).json()
    await owner.patch(f"/appointments/{appt['id']}", json={"status": "COMPLETED"})
    url = f"/appointments/{appt['id']}/feedback"

    r = await bot.post(url, json={"submitted_by": "AGENT", "interest_score": 5})
    assert r.status_code == 403

    body = {"submitted_by": "CLIENT", "interest_score": 4, "objection": "PRICE"}
    first = await bot.post(url, json=body)
    assert first.status_code == 201, first.text
    again = await bot.post(url, json=body)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]

    rows = (await bot.get(url)).json()
    assert [f["submitted_by"] for f in rows] == ["CLIENT"]

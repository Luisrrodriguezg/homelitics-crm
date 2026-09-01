"""HU-05 slot computation: weekly rules minus time off minus blocking visits.

compute_slots and the appointment overlap check must agree about what occupies a
calendar — that is why the service imports `_BLOCKING` rather than re-listing it.
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from app.models import AgentTimeOff, Appointment
from app.schemas import AvailabilityCreate
from app.services import availability as svc

# America/Bogota is UTC-5 year round (no DST): 09:00 local == 14:00Z.
MON = datetime(2026, 9, 7, tzinfo=timezone.utc)          # a Monday
WINDOW = (MON, MON + timedelta(days=5))                  # Mon–Fri


async def _rule(session, agent, weekday, start=time(9), end=time(12)):
    return await svc.add_availability(
        session, agent_id=agent.id, agency_id=agent.agency_id,
        data=AvailabilityCreate(weekday=weekday, start_time=start, end_time=end),
    )


async def test_weekly_rule_expands_to_a_30_minute_grid(world, session):
    agent = world.agents[0][0]
    for wd in range(5):
        await _rule(session, agent, wd)

    slots = await svc.compute_slots(
        session, agent_id=agent.id, agency_id=agent.agency_id,
        start=WINDOW[0], end=WINDOW[1],
    )
    # 09:00,09:30,10:00,10:30,11:00,11:30 on each of Mon–Fri
    assert len(slots) == 5 * 6
    assert all(s.minute in (0, 30) for s in slots)
    assert slots == sorted(slots)


@pytest.mark.parametrize("kind", ["time_off", "appointment"])
async def test_a_busy_block_removes_the_overlapping_slot(world, session, kind):
    agent = world.agents[0][0]
    await _rule(session, agent, MON.weekday())

    # 10:00–10:30 Bogota == 15:00Z on that Monday
    busy_start = MON.replace(hour=15)
    if kind == "time_off":
        session.add(AgentTimeOff(
            agent_id=agent.id, starts_at=busy_start,
            ends_at=busy_start + timedelta(minutes=30), reason="dentist",
        ))
    else:
        from app.models import Lead  # an appointment needs a lead
        lead = Lead(
            client_id=world.clients[0].id, listing_id=world.listings[0].id,
            agent_id=agent.id, source_channel="IN_APP", current_stage="INTERESTED",
        )
        session.add(lead)
        await session.flush()
        session.add(Appointment(
            lead_id=lead.id, agent_id=agent.id, scheduled_at=busy_start,
            duration_min=30, status="CONFIRMED",
        ))
    # Commit: the `world` teardown runs before the session fixture closes, so an
    # open transaction here would lock its DELETEs. Teardown deletes these rows
    # by agent scope.
    await session.commit()

    slots = await svc.compute_slots(
        session, agent_id=agent.id, agency_id=agent.agency_id,
        start=MON, end=MON + timedelta(days=1),
    )
    assert busy_start not in slots
    assert busy_start - timedelta(minutes=30) in slots  # 09:30 still free

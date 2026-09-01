"""Agent availability (HU-05) and slot computation.

CLAUDE.md originally cut the availability tables; 003_availability.sql brings
them back as the minimum: a weekly rule set plus ad-hoc time off. Turning that
into bookable slots is this module's job.

`compute_slots` and the appointment overlap check must never disagree about what
occupies a calendar, so this module imports `_BLOCKING` from
`app.services.appointment` rather than re-listing the statuses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Agent, AgentAvailability, AgentTimeOff, Appointment
from app.services.appointment import _BLOCKING

SLOT_MINUTES = 30


async def _agent_in_agency(
    session: AsyncSession, agent_id: uuid.UUID, agency_id: uuid.UUID
) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.id == agent_id, Agent.agency_id == agency_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found in this agency")
    return agent


# --------------------------------------------------------------- weekly rules

async def list_availability(
    session: AsyncSession, *, agent_id: uuid.UUID, agency_id: uuid.UUID
) -> list[AgentAvailability]:
    await _agent_in_agency(session, agent_id, agency_id)
    return list(
        (
            await session.execute(
                select(AgentAvailability)
                .where(AgentAvailability.agent_id == agent_id)
                .order_by(AgentAvailability.weekday, AgentAvailability.start_time)
            )
        ).scalars().all()
    )


async def add_availability(
    session: AsyncSession, *, agent_id: uuid.UUID, agency_id: uuid.UUID, data
) -> AgentAvailability:
    await _agent_in_agency(session, agent_id, agency_id)
    if data.start_time >= data.end_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_time must be before end_time")
    row = AgentAvailability(
        agent_id=agent_id,
        weekday=data.weekday,
        start_time=data.start_time,
        end_time=data.end_time,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def patch_availability(
    session: AsyncSession, *, agent_id: uuid.UUID, rule_id: uuid.UUID, agency_id: uuid.UUID, data
) -> AgentAvailability:
    await _agent_in_agency(session, agent_id, agency_id)
    row = (
        await session.execute(
            select(AgentAvailability).where(
                AgentAvailability.id == rule_id, AgentAvailability.agent_id == agent_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability rule not found")
    for field in ("weekday", "start_time", "end_time", "valid_from", "valid_to"):
        value = getattr(data, field)
        if value is not None:
            setattr(row, field, value)
    if row.start_time >= row.end_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_time must be before end_time")
    await session.commit()
    await session.refresh(row)
    return row


async def delete_availability(
    session: AsyncSession, *, agent_id: uuid.UUID, rule_id: uuid.UUID, agency_id: uuid.UUID
) -> None:
    await _agent_in_agency(session, agent_id, agency_id)
    row = (
        await session.execute(
            select(AgentAvailability).where(
                AgentAvailability.id == rule_id, AgentAvailability.agent_id == agent_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability rule not found")
    await session.delete(row)
    await session.commit()


# ------------------------------------------------------------------ time off

async def list_time_off(
    session: AsyncSession, *, agent_id: uuid.UUID, agency_id: uuid.UUID
) -> list[AgentTimeOff]:
    await _agent_in_agency(session, agent_id, agency_id)
    return list(
        (
            await session.execute(
                select(AgentTimeOff)
                .where(AgentTimeOff.agent_id == agent_id)
                .order_by(AgentTimeOff.starts_at)
            )
        ).scalars().all()
    )


async def add_time_off(
    session: AsyncSession, *, agent_id: uuid.UUID, agency_id: uuid.UUID, data
) -> AgentTimeOff:
    await _agent_in_agency(session, agent_id, agency_id)
    if data.starts_at >= data.ends_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "starts_at must be before ends_at")
    row = AgentTimeOff(
        agent_id=agent_id, starts_at=data.starts_at, ends_at=data.ends_at, reason=data.reason
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_time_off(
    session: AsyncSession, *, agent_id: uuid.UUID, off_id: uuid.UUID, agency_id: uuid.UUID
) -> None:
    await _agent_in_agency(session, agent_id, agency_id)
    row = (
        await session.execute(
            select(AgentTimeOff).where(
                AgentTimeOff.id == off_id, AgentTimeOff.agent_id == agent_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Time-off entry not found")
    await session.delete(row)
    await session.commit()


# ------------------------------------------------------------------ slots

def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


async def compute_slots(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    """Free 30-minute slot starts (UTC) for the agent between `start` and `end`.

    Weekly rules are expanded in APP_TIMEZONE (agents publish in local time),
    then time off and calendar-blocking appointments are subtracted.
    """
    await _agent_in_agency(session, agent_id, agency_id)
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`from` must be before `to`")

    tz = ZoneInfo(get_settings().app_timezone)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)

    rules = (
        await session.execute(
            select(AgentAvailability).where(AgentAvailability.agent_id == agent_id)
        )
    ).scalars().all()

    time_off = (
        await session.execute(
            select(AgentTimeOff.starts_at, AgentTimeOff.ends_at).where(
                AgentTimeOff.agent_id == agent_id,
                AgentTimeOff.starts_at < end,
                AgentTimeOff.ends_at > start,
            )
        )
    ).all()

    appts = (
        await session.execute(
            select(Appointment.scheduled_at, Appointment.duration_min).where(
                Appointment.agent_id == agent_id,
                Appointment.status.in_(_BLOCKING),
            )
        )
    ).all()
    busy = [(s, s + timedelta(minutes=d)) for s, d in appts]
    busy += [(s, e) for s, e in time_off]

    step = timedelta(minutes=SLOT_MINUTES)
    slots: list[datetime] = []

    # Walk local calendar days across the range.
    day = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    while day <= last_day:
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            if rule.valid_from and day < rule.valid_from:
                continue
            if rule.valid_to and day > rule.valid_to:
                continue
            cursor = datetime.combine(day, rule.start_time, tzinfo=tz)
            block_end = datetime.combine(day, rule.end_time, tzinfo=tz)
            while cursor + step <= block_end:
                s_utc = cursor.astimezone(timezone.utc)
                e_utc = s_utc + step
                if start <= s_utc and e_utc <= end and not any(
                    _overlaps(s_utc, e_utc, b0, b1) for b0, b1 in busy
                ):
                    slots.append(s_utc)
                cursor += step
        day += timedelta(days=1)

    return sorted(set(slots))


async def slot_is_available(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    scheduled_at: datetime,
    duration_min: int,
) -> bool:
    """True iff [scheduled_at, +duration) is fully covered by published free slots."""
    end = scheduled_at + timedelta(minutes=duration_min)
    free = set(await compute_slots(
        session, agent_id=agent_id, agency_id=agency_id, start=scheduled_at, end=end
    ))
    step = timedelta(minutes=SLOT_MINUTES)
    needed = scheduled_at
    while needed < end:
        if needed not in free:
            return False
        needed += step
    return True

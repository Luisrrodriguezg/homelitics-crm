"""Agent availability (HU-05) and slot computation.

CLAUDE.md originally cut the availability tables; 003_availability.sql brings
them back as the minimum: a weekly rule set plus ad-hoc time off. Turning that
into bookable slots is this module's job.

`compute_slots` and the appointment overlap check must never disagree about what
occupies a calendar, so this module imports `_BLOCKING` from
`app.services.appointment` rather than re-listing the statuses.

Time off comes from two places (009): typed in here (source MANUAL) or imported
from the agent's own calendar (source ICS, services/calendar_import). Slot maths
treats them the same; only MANUAL rows are edited through this module.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Agent, AgentAvailability, AgentTimeOff, Appointment
from app.services import calendar_import
from app.services.appointment import _BLOCKING, lock_agent_calendar

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
    """Book time off — but never over a visit that is still on the calendar.

    A booked visit is a commitment to a client, so the agent moves or cancels
    it first (and tells the client). Same per-agent lock as booking, so a
    visit cannot land in the window while this runs.
    """
    await _agent_in_agency(session, agent_id, agency_id)
    if data.starts_at >= data.ends_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "starts_at must be before ends_at")

    await lock_agent_calendar(session, agent_id)
    appt_end = Appointment.scheduled_at + func.make_interval(
        0, 0, 0, 0, 0, Appointment.duration_min
    )
    clashes = (
        await session.execute(
            select(Appointment.id, Appointment.scheduled_at)
            .where(
                Appointment.agent_id == agent_id,
                Appointment.status.in_(_BLOCKING),
                Appointment.scheduled_at < data.ends_at,
                appt_end > data.starts_at,
            )
            .order_by(Appointment.scheduled_at)
        )
    ).all()
    if clashes:
        listed = ", ".join(f"{a_id} at {at.isoformat()}" for a_id, at in clashes)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Time off overlaps {len(clashes)} booked visit(s): {listed}. "
            "Move or cancel them first.",
        )

    row = AgentTimeOff(
        agent_id=agent_id, starts_at=data.starts_at, ends_at=data.ends_at,
        reason=data.reason, source="MANUAL",
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
    if row.source != "MANUAL":
        # The next sync would only bring it back.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This block comes from the agent's external calendar; remove it there "
            "(or disconnect the calendar)",
        )
    await session.delete(row)
    await session.commit()


# ------------------------------------------------------------------ slots

def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def expand_rules(
    rules, start: datetime, end: datetime, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Weekly rules as concrete UTC blocks, for every local day `[start, end]` touches.

    Blocks are *not* clipped to the window: the slot grid is anchored on each
    block's own start (09:00, 09:30, ...), whatever window the caller asked for.
    """
    blocks: list[tuple[datetime, datetime]] = []
    day: date = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    while day <= last_day:
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            if rule.valid_from and day < rule.valid_from:
                continue
            if rule.valid_to and day > rule.valid_to:
                continue
            blocks.append((
                datetime.combine(day, rule.start_time, tzinfo=tz).astimezone(timezone.utc),
                datetime.combine(day, rule.end_time, tzinfo=tz).astimezone(timezone.utc),
            ))
        day += timedelta(days=1)
    return blocks


async def compute_slots(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    start: datetime,
    end: datetime,
    duration_min: int = SLOT_MINUTES,
    not_before: datetime | None = None,
    exclude_appointment_id: uuid.UUID | None = None,
) -> list[datetime]:
    """Slot starts (UTC) between `start` and `end` where a visit of
    `duration_min` fits entirely in free time.

    Weekly rules are expanded in APP_TIMEZONE (agents publish in local time)
    into a 30-minute grid; time off (manual and imported) and calendar-blocking
    appointments are subtracted; a start qualifies when every cell the visit
    covers is free. `exclude_appointment_id` lets a visit be moved within its
    own slot. Runs under the booking lock, so it never fetches anything.
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

    # Only the window: without the time predicates this read the agent's whole
    # history and could not use idx_appointment_agent (agent_id, scheduled_at).
    appt_end = Appointment.scheduled_at + func.make_interval(
        0, 0, 0, 0, 0, Appointment.duration_min
    )
    appt_q = select(Appointment.scheduled_at, Appointment.duration_min).where(
        Appointment.agent_id == agent_id,
        Appointment.status.in_(_BLOCKING),
        Appointment.scheduled_at < end,
        appt_end > start,
    )
    if exclude_appointment_id is not None:
        appt_q = appt_q.where(Appointment.id != exclude_appointment_id)
    appts = (await session.execute(appt_q)).all()

    busy = [(s, s + timedelta(minutes=d)) for s, d in appts]
    busy += [(s, e) for s, e in time_off]

    step = timedelta(minutes=SLOT_MINUTES)
    free: set[datetime] = set()
    for block_start, block_end in expand_rules(rules, start, end, tz):
        cursor = block_start
        while cursor + step <= block_end:
            cell_end = cursor + step
            if start <= cursor and cell_end <= end and not any(
                _overlaps(cursor, cell_end, b0, b1) for b0, b1 in busy
            ):
                free.add(cursor)
            cursor += step

    cells = math.ceil(duration_min / SLOT_MINUTES)
    return sorted(
        s for s in free
        if (not_before is None or s >= not_before)
        and all(s + k * step in free for k in range(1, cells))
    )


async def free_slots(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    start: datetime,
    end: datetime,
    duration_min: int,
) -> list[datetime]:
    """GET /agents/{id}/slots: refresh imported busy time, then offer only
    future starts. The refresh runs here, outside any lock, never inside
    compute_slots."""
    await _agent_in_agency(session, agent_id, agency_id)
    await calendar_import.maybe_refresh(session, agent_id=agent_id)
    return await compute_slots(
        session, agent_id=agent_id, agency_id=agency_id, start=start, end=end,
        duration_min=duration_min, not_before=datetime.now(timezone.utc),
    )


async def slot_is_available(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    scheduled_at: datetime,
    duration_min: int,
    exclude_appointment_id: uuid.UUID | None = None,
) -> bool:
    """True iff [scheduled_at, +duration) is fully covered by published free slots."""
    end = scheduled_at + timedelta(minutes=duration_min)
    fits = await compute_slots(
        session, agent_id=agent_id, agency_id=agency_id, start=scheduled_at, end=end,
        duration_min=duration_min, exclude_appointment_id=exclude_appointment_id,
    )
    return scheduled_at.astimezone(timezone.utc) in fits

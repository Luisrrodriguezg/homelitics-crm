"""Appointments, including the overlap check the schema deliberately does not do.

schema-2.sql cut the `EXCLUDE USING gist` constraint because an inline tstzrange
built from (scheduled_at, duration_min) is not IMMUTABLE and the constraint will
not create. Preventing double-booking is therefore this module's job, and doing
it correctly requires a row lock, not just a SELECT — see _assert_no_overlap.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Agent, Appointment, Lead, Objection, VisitFeedback
from app.services import events
from app.services.lead import get_lead

# What a client may ask for directly. COMPLETED/NO_SHOW are outcomes recorded
# after the fact; CONFIRMED/RESCHEDULED/CANCELLED are the agent's decisions.
_TERMINAL_APPOINTMENT = frozenset({"CANCELLED", "COMPLETED", "NO_SHOW"})

# Statuses that still occupy the agent's calendar. A cancelled visit does not
# block a new one at the same time.
_BLOCKING = ("PENDING_CONFIRMATION", "CONFIRMED", "RESCHEDULED")


async def _assert_no_overlap(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    start: datetime,
    duration_min: int,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Reject a booking that overlaps one the agent already has.

    `with_for_update()` is the load-bearing part. Without the row lock, two
    concurrent requests both read "no overlap" and both insert — the exact race
    the dropped EXCLUDE constraint would have prevented. Locking the agent's
    candidate rows serialises those two transactions.
    """
    # A `SELECT ... FOR UPDATE` that matches no rows locks nothing, so for a
    # brand-new slot two concurrent bookers both see "no overlap" and both
    # insert. A per-agent, transaction-scoped advisory lock closes that gap:
    # the racers queue here and each sees the previous one's committed row.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended('appt:' || :aid, 0))"),
        {"aid": str(agent_id)},
    )

    end = start + timedelta(minutes=duration_min)
    appt_end = Appointment.scheduled_at + func.make_interval(
        0, 0, 0, 0, 0, Appointment.duration_min
    )

    q = (
        select(Appointment.id, Appointment.scheduled_at, Appointment.duration_min)
        .where(
            Appointment.agent_id == agent_id,
            Appointment.status.in_(_BLOCKING),
            # half-open intervals: [start, end) — touching at the boundary is fine
            Appointment.scheduled_at < end,
            appt_end > start,
        )
        .with_for_update()
    )
    if exclude_id is not None:
        q = q.where(Appointment.id != exclude_id)

    clash = (await session.execute(q)).first()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Agent already has an appointment overlapping that slot "
            f"(existing appointment {clash[0]} at {clash[1].isoformat()}, "
            f"{clash[2]} min)",
        )


async def request_visit(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    scheduled_at: datetime,
    duration_min: int,
    agent: Agent,
) -> Appointment:
    """HU-02: *request* a visit. Availability tables were cut, so the flow is
    request -> agent confirms, not book-a-free-slot."""
    lead = await get_lead(session, lead_id, agent.agency_id)

    if scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "scheduled_at must be in the future"
        )

    await _assert_no_overlap(
        session, agent_id=lead.agent_id, start=scheduled_at, duration_min=duration_min
    )

    # Optional: reject a slot the agent has not published. Off by default so the
    # overlap tests keep their meaning; on, it enforces HU-05 against HU-02.
    if get_settings().enforce_availability:
        from app.services.availability import slot_is_available  # lazy: import cycle

        if not await slot_is_available(
            session, agent_id=lead.agent_id, agency_id=agent.agency_id,
            scheduled_at=scheduled_at, duration_min=duration_min,
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That slot is outside the agent's published availability",
            )

    appointment = Appointment(
        lead_id=lead_id,
        agent_id=lead.agent_id,
        scheduled_at=scheduled_at,
        duration_min=duration_min,
        status="PENDING_CONFIRMATION",
    )
    session.add(appointment)
    await session.flush()
    events.emit(
        session,
        event_type="appointment.booked",
        aggregate_type="appointment",
        aggregate_id=appointment.id,
        agency_id=agent.agency_id,
        payload={"lead_id": str(lead_id), "agent_id": str(lead.agent_id),
                 "scheduled_at": scheduled_at.isoformat()},
    )
    await session.commit()
    await session.refresh(appointment)
    return appointment


async def get_appointment(
    session: AsyncSession, *, appointment_id: uuid.UUID, agency_id: uuid.UUID
) -> Appointment:
    appointment = (
        await session.execute(
            select(Appointment)
            .join(Lead, Lead.id == Appointment.lead_id)
            .join(Agent, Agent.id == Lead.agent_id)
            .where(Appointment.id == appointment_id, Agent.agency_id == agency_id)
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appointment


async def list_appointments(
    session: AsyncSession, *, lead_id: uuid.UUID, agency_id: uuid.UUID
) -> list[Appointment]:
    await get_lead(session, lead_id, agency_id)
    return list(
        (
            await session.execute(
                select(Appointment)
                .where(Appointment.lead_id == lead_id)
                .order_by(Appointment.scheduled_at)
            )
        ).scalars().all()
    )


async def patch_appointment(
    session: AsyncSession, *, appointment_id: uuid.UUID, agent: Agent, data
) -> Appointment:
    """Confirm / reschedule / cancel / complete / no-show."""
    appointment = await get_appointment(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )

    if appointment.status in _TERMINAL_APPOINTMENT and data.status != appointment.status:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Appointment is already {appointment.status} and cannot be changed",
        )

    new_start = data.scheduled_at or appointment.scheduled_at
    new_duration = data.duration_min or appointment.duration_min

    if data.scheduled_at is not None or data.duration_min is not None:
        if new_start <= datetime.now(timezone.utc):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "scheduled_at must be in the future"
            )
        await _assert_no_overlap(
            session,
            agent_id=appointment.agent_id,
            start=new_start,
            duration_min=new_duration,
            exclude_id=appointment.id,
        )
        appointment.scheduled_at = new_start
        appointment.duration_min = new_duration
        # Moving a confirmed visit reopens it unless the caller says otherwise.
        if data.status is None and appointment.status == "CONFIRMED":
            appointment.status = "RESCHEDULED"

    if data.status is not None:
        appointment.status = data.status

    appointment.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(appointment)
    return appointment


async def add_feedback(
    session: AsyncSession, *, appointment_id: uuid.UUID, agent: Agent, data
) -> VisitFeedback:
    appointment = await get_appointment(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )
    if appointment.status != "COMPLETED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Feedback belongs on a COMPLETED visit; this one is {appointment.status}",
        )

    objection_id = None
    if data.objection:
        objection_id = await session.scalar(
            select(Objection.id).where(Objection.code == data.objection)
        )
        if objection_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown objection {data.objection!r}"
            )

    feedback = VisitFeedback(
        appointment_id=appointment_id,
        submitted_by=data.submitted_by,
        interest_score=data.interest_score,
        objection_id=objection_id,
        close_probability=data.close_probability,
        free_text=data.free_text,
    )
    session.add(feedback)
    await session.commit()
    await session.refresh(feedback)
    return feedback

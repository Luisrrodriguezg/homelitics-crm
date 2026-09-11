"""Appointments — the visit calendar — and the rules around it.

core.appointment *is* the calendar: one row per visit (lead, agent, time,
status). services/calendar.py renders it, services/availability.py and
services/calendar_import.py constrain it; the decisions live here.
docs/DECISIONS.md §19.

* **Status is the owning agent's consent.** A visit the lead's owner books is
  born CONFIRMED. One booked by anybody else — an AI agent on the client's
  behalf, a colleague — is PENDING_CONFIRMATION until the owner confirms (HU-02).
  An AI agent granted `visits:manage` books CONFIRMED: the instant-booking switch.
* **AI agents book only what /slots offers**, and never at short notice.
* **One open visit per lead** (UNIQUE partial index, 009). Posting the same slot
  again returns the visit already made, so a bot's retry is safe.
* **The calendar drives the funnel.** CONFIRMED moves an INTERESTED lead to
  VISIT_SCHEDULED, COMPLETED moves VISIT_SCHEDULED to VISITED, and closing a
  lead (WON/LOST, services/lead.py) cancels its open visits.

Overlap: schema-2.sql cut the `EXCLUDE USING gist` constraint because an inline
tstzrange built from (scheduled_at, duration_min) is not IMMUTABLE and the
constraint will not create. Preventing double-booking is therefore this
module's job, and doing it correctly requires a lock, not just a SELECT — see
lock_agent_calendar and _assert_no_overlap.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import has_scope
from app.models import Agent, Appointment, Interaction, Lead, Objection, VisitFeedback
from app.schemas import TERMINAL_STAGES
from app.services import calendar_import, events
from app.services.lead import get_lead, insert_transition

# COMPLETED/NO_SHOW are outcomes recorded after the fact; CONFIRMED/RESCHEDULED/
# CANCELLED are decisions. None of the three terminal ones can be left again.
_TERMINAL_APPOINTMENT = frozenset({"CANCELLED", "COMPLETED", "NO_SHOW"})

# Statuses that still occupy the agent's calendar. A cancelled visit does not
# block a new one at the same time. uq_appointment_open_per_lead (009) repeats
# this list in SQL — keep the two in step.
_BLOCKING = ("PENDING_CONFIRMATION", "CONFIRMED", "RESCHEDULED")

# Outcomes only a person who was there can record — never an AI agent.
HUMAN_ONLY_STATUSES = frozenset({"COMPLETED", "NO_SHOW"})

_EVENT_FOR_STATUS = {
    "CONFIRMED": "appointment.confirmed",
    "CANCELLED": "appointment.cancelled",
    "COMPLETED": "appointment.completed",
    "NO_SHOW": "appointment.no_show",
    "RESCHEDULED": "appointment.rescheduled",
    "PENDING_CONFIRMATION": "appointment.reopened",
}


# ------------------------------------------------------------------ locking

async def lock_agent_calendar(session: AsyncSession, agent_id: uuid.UUID) -> None:
    """Serialise every write to one agent's calendar until this transaction ends.

    A `SELECT ... FOR UPDATE` that matches no rows locks nothing, so for a
    brand-new slot two concurrent bookers both see "no overlap" and both
    insert. A per-agent, transaction-scoped advisory lock closes that gap: the
    racers queue here and each sees the previous one's committed row. Booking,
    moving and manual time off all take it. Re-entrant within a transaction,
    and xact-scoped, so it releases on commit — safe under the transaction
    pooler, where a session-scoped lock would not be.
    """
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended('appt:' || :aid, 0))"),
        {"aid": str(agent_id)},
    )


async def _assert_no_overlap(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    start: datetime,
    duration_min: int,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Reject a booking that overlaps one the agent already has.

    The advisory lock serialises bookers; `with_for_update()` on the rows that
    do exist stops a concurrent reschedule from moving one out from under this
    check.
    """
    await lock_agent_calendar(session, agent_id)

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


# ------------------------------------------------------------------ helpers

def _local(ts: datetime) -> str:
    """How a time reads on the lead's timeline: the agents' own clock."""
    return ts.astimezone(ZoneInfo(get_settings().app_timezone)).strftime("%Y-%m-%d %H:%M")


def _check_notice(start: datetime) -> None:
    """AI agents never book or move a visit to start at short notice."""
    minutes = get_settings().visit_min_notice_minutes
    earliest = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    if start < earliest:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"An AI agent books at least {minutes} min ahead; the earliest start "
            f"is {earliest.replace(microsecond=0).isoformat()}",
        )


async def _assert_in_published_slots(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agency_id: uuid.UUID,
    start: datetime,
    duration_min: int,
    exclude_id: uuid.UUID | None = None,
) -> None:
    from app.services.availability import slot_is_available  # lazy: import cycle

    if not await slot_is_available(
        session, agent_id=agent_id, agency_id=agency_id, scheduled_at=start,
        duration_min=duration_min, exclude_appointment_id=exclude_id,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That slot is outside the agent's published availability (see "
            "GET /agents/{agent_id}/slots)",
        )


async def _open_visit(session: AsyncSession, lead_id: uuid.UUID) -> Appointment | None:
    return (
        await session.execute(
            select(Appointment).where(
                Appointment.lead_id == lead_id, Appointment.status.in_(_BLOCKING)
            )
        )
    ).scalars().first()


async def _sync_funnel(
    session: AsyncSession, *, lead_id: uuid.UUID, visit_status: str, actor: Agent
) -> None:
    """The calendar drives the funnel, forward only, along edges the funnel
    already allows. The stage is read with a plain column select — the ORM's
    cached Lead may predate a trigger write in this same request."""
    stage = await session.scalar(select(Lead.current_stage).where(Lead.id == lead_id))
    target = {
        ("CONFIRMED", "INTERESTED"): "VISIT_SCHEDULED",
        ("COMPLETED", "VISIT_SCHEDULED"): "VISITED",
    }.get((visit_status, stage))
    if target is not None:
        insert_transition(
            session, lead_id=lead_id, from_stage=stage, to_stage=target,
            actor=actor, agency_id=actor.agency_id,
        )


def _record(
    session: AsyncSession,
    *,
    appointment: Appointment,
    actor: Agent,
    agency_id: uuid.UUID,
    event_type: str,
    note: str,
    extra: dict | None = None,
) -> None:
    """One timeline line and one outbox event for every change to a visit.

    The line is a STATUS_CHANGE attributed to whoever acted: an agent opening
    the lead sees what the bot did, and a bot's calendar writes count toward
    its hourly budget — without ever counting as an agent *response*
    (schemas.RESPONSE_TYPES is MESSAGE and CALL only).
    """
    session.add(
        Interaction(
            lead_id=appointment.lead_id, direction="OUTBOUND", channel="IN_APP",
            type="STATUS_CHANGE", body=note, created_by=actor.id,
        )
    )
    events.emit(
        session,
        event_type=event_type,
        aggregate_type="appointment",
        aggregate_id=appointment.id,
        agency_id=agency_id,
        payload={
            "lead_id": str(appointment.lead_id),
            "agent_id": str(appointment.agent_id),
            "scheduled_at": appointment.scheduled_at.isoformat(),
            "duration_min": appointment.duration_min,
            "status": appointment.status,
            "actor_id": str(actor.id),
            "by_bot": actor.is_bot,
            **(extra or {}),
        },
    )


# ------------------------------------------------------------------ booking

async def request_visit(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    scheduled_at: datetime,
    duration_min: int,
    agent: Agent,
) -> tuple[Appointment, bool]:
    """HU-02: book a visit. Returns (appointment, created).

    `created` is False when this lead already has an open visit at exactly this
    slot: a retried POST gets back the visit it already made (200) instead of a
    409 from its own booking. Any *other* open visit on the lead is a 409 —
    move that one with PATCH rather than stacking a second.
    """
    settings = get_settings()
    lead = await get_lead(session, lead_id, agent.agency_id)

    if lead.current_stage in TERMINAL_STAGES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Lead is {lead.current_stage}; a closed lead takes no visits",
        )
    if scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "scheduled_at must be in the future"
        )
    if agent.is_bot:
        _check_notice(scheduled_at)

    must_fit = agent.is_bot or settings.enforce_availability
    if must_fit:
        # Busy time from the agent's own calendar, fetched before any lock is
        # held: it can be a network round trip.
        await calendar_import.maybe_refresh(session, agent_id=lead.agent_id)

    await lock_agent_calendar(session, lead.agent_id)

    existing = await _open_visit(session, lead_id)
    if existing is not None:
        if existing.scheduled_at == scheduled_at and existing.duration_min == duration_min:
            return existing, False
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Lead already has an open visit ({existing.id}, {existing.status}) at "
            f"{existing.scheduled_at.isoformat()}; PATCH /appointments/{existing.id} "
            "to move it",
        )

    await _assert_no_overlap(
        session, agent_id=lead.agent_id, start=scheduled_at, duration_min=duration_min
    )
    if must_fit:
        await _assert_in_published_slots(
            session, agent_id=lead.agent_id, agency_id=agent.agency_id,
            start=scheduled_at, duration_min=duration_min,
        )

    # Consent: the owner booking it has agreed by definition; a bot holding
    # visits:manage has been trusted to agree for them.
    confirmed = (
        (agent.is_bot and has_scope(agent, "visits:manage"))
        or (not agent.is_bot and agent.id == lead.agent_id)
    )
    appointment = Appointment(
        lead_id=lead_id,
        agent_id=lead.agent_id,
        scheduled_at=scheduled_at,
        duration_min=duration_min,
        status="CONFIRMED" if confirmed else "PENDING_CONFIRMATION",
        created_by=agent.id,
    )
    session.add(appointment)
    try:
        await session.flush()
    except IntegrityError:
        # uq_appointment_open_per_lead: only reachable if the lead changed
        # hands mid-request, since the lock above serialises the same agent.
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Lead already has an open visit")

    if confirmed:
        await _sync_funnel(session, lead_id=lead_id, visit_status="CONFIRMED", actor=agent)
    _record(
        session, appointment=appointment, actor=agent, agency_id=agent.agency_id,
        event_type="appointment.booked",
        note=(f"Visit {'booked' if confirmed else 'requested'} for "
              f"{_local(scheduled_at)} ({duration_min} min)"
              + ("" if confirmed else " — awaiting the agent's confirmation")),
    )
    await session.commit()
    await session.refresh(appointment)
    return appointment, True


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
    """Confirm / reschedule / cancel / complete / no-show — and the funnel follows."""
    settings = get_settings()
    appointment = await get_appointment(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )

    if appointment.status in _TERMINAL_APPOINTMENT and data.status != appointment.status:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Appointment is already {appointment.status} and cannot be changed",
        )

    # The router already refuses these with a 403; repeated so no other caller
    # of this function can skip them.
    if agent.is_bot:
        if data.status in HUMAN_ONLY_STATUSES:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only a person who was there records a visit's outcome")
        if data.status == "CONFIRMED" and not has_scope(agent, "visits:manage"):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Service account lacks the 'visits:manage' scope")

    old_status, old_start = appointment.status, appointment.scheduled_at
    old_duration = appointment.duration_min
    new_start = data.scheduled_at or appointment.scheduled_at
    new_duration = data.duration_min or appointment.duration_min
    moving = data.scheduled_at is not None or data.duration_min is not None

    if moving:
        if new_start <= datetime.now(timezone.utc):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "scheduled_at must be in the future"
            )
        if agent.is_bot:
            _check_notice(new_start)
        must_fit = agent.is_bot or settings.enforce_availability
        if must_fit:
            await calendar_import.maybe_refresh(session, agent_id=appointment.agent_id)
        await _assert_no_overlap(
            session,
            agent_id=appointment.agent_id,
            start=new_start,
            duration_min=new_duration,
            exclude_id=appointment.id,
        )
        if must_fit:
            await _assert_in_published_slots(
                session, agent_id=appointment.agent_id, agency_id=agent.agency_id,
                start=new_start, duration_min=new_duration, exclude_id=appointment.id,
            )
        time_changed = new_start != old_start or new_duration != old_duration
        appointment.scheduled_at = new_start
        appointment.duration_min = new_duration
        # Moving a confirmed visit reopens it unless the caller says otherwise:
        # the owner confirms their own move by sending status CONFIRMED with it.
        # Re-sending the current time (a retry) changes nothing.
        if data.status is None and appointment.status == "CONFIRMED" and time_changed:
            appointment.status = "RESCHEDULED"

    if data.status is not None:
        appointment.status = data.status

    moved = appointment.scheduled_at != old_start or appointment.duration_min != old_duration
    if not moved and appointment.status == old_status:
        return appointment          # nothing to change, nothing to record

    appointment.updated_at = datetime.now(timezone.utc)
    if appointment.status != old_status and appointment.status in ("CONFIRMED", "COMPLETED"):
        await _sync_funnel(
            session, lead_id=appointment.lead_id, visit_status=appointment.status, actor=agent
        )

    if moved:
        event_type = "appointment.rescheduled"
        note = f"Visit moved from {_local(old_start)} to {_local(appointment.scheduled_at)}"
        if appointment.status != old_status:
            note += f" ({appointment.status})"
    else:
        event_type = _EVENT_FOR_STATUS[appointment.status]
        note = f"Visit on {_local(appointment.scheduled_at)}: {old_status} -> {appointment.status}"
    _record(
        session, appointment=appointment, actor=agent, agency_id=agent.agency_id,
        event_type=event_type, note=note,
        extra={"previous_status": old_status, "previous_scheduled_at": old_start.isoformat()},
    )
    await session.commit()
    await session.refresh(appointment)
    return appointment


async def cancel_open_visits(
    session: AsyncSession, *, lead_id: uuid.UUID, actor: Agent, closed_as: str
) -> int:
    """A closed lead keeps no visits: free the agent's slot. Called by
    services/lead.add_transition inside its transaction; does not commit."""
    rows = (
        await session.execute(
            select(Appointment).where(
                Appointment.lead_id == lead_id, Appointment.status.in_(_BLOCKING)
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for appointment in rows:
        previous = appointment.status
        appointment.status = "CANCELLED"
        appointment.updated_at = now
        _record(
            session, appointment=appointment, actor=actor, agency_id=actor.agency_id,
            event_type="appointment.cancelled",
            note=f"Visit on {_local(appointment.scheduled_at)} cancelled: lead moved to {closed_as}",
            extra={"previous_status": previous, "reason": f"lead_{closed_as.lower()}"},
        )
    return len(rows)


# ------------------------------------------------------------------ feedback

async def list_feedback(
    session: AsyncSession, *, appointment_id: uuid.UUID, agency_id: uuid.UUID
) -> list[VisitFeedback]:
    await get_appointment(session, appointment_id=appointment_id, agency_id=agency_id)
    return list(
        (
            await session.execute(
                select(VisitFeedback)
                .where(VisitFeedback.appointment_id == appointment_id)
                .order_by(VisitFeedback.created_at)
            )
        ).scalars().all()
    )


async def add_feedback(
    session: AsyncSession, *, appointment_id: uuid.UUID, agent: Agent, data
) -> tuple[VisitFeedback, bool]:
    """Returns (feedback, created). One row per side per visit
    (uq_visit_feedback_side): a second post from the same side returns the
    first row, so a bot retrying after a timeout does not double-count."""
    appointment = await get_appointment(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )
    if agent.is_bot and data.submitted_by != "CLIENT":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "An AI agent records the client's feedback only (submitted_by CLIENT)",
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

    new_id = (
        await session.execute(
            insert(VisitFeedback)
            .values(
                appointment_id=appointment_id,
                submitted_by=data.submitted_by,
                interest_score=data.interest_score,
                objection_id=objection_id,
                close_probability=data.close_probability,
                free_text=data.free_text,
            )
            .on_conflict_do_nothing(
                index_elements=[VisitFeedback.appointment_id, VisitFeedback.submitted_by]
            )
            .returning(VisitFeedback.id)
        )
    ).scalar_one_or_none()
    await session.commit()

    feedback = (
        await session.execute(
            select(VisitFeedback).where(
                VisitFeedback.appointment_id == appointment_id,
                VisitFeedback.submitted_by == data.submitted_by,
            )
        )
    ).scalar_one()
    return feedback, new_id is not None

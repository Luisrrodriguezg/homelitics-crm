"""Lead funnel logic.

Every function here takes an explicit agency_id and filters on it. That is the
tenancy boundary — there is no RLS behind this. A lead belonging to another
agency is reported as 404, not 403: existence itself is not the caller's
business.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import Select, exists, func, or_, select, true
# postgresql.insert, not sqlalchemy.insert: on_conflict_do_nothing is dialect-specific.
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.models import (
    Agent, AssignmentAudit, Client, FollowUpTask, Interaction, Lead,
    LeadLostDetail, LeadStage, LeadStageTransition, Listing, LostReason, Person,
    Property,
)
from app.schemas import ALLOWED_TRANSITIONS, RESPONSE_TYPES, TERMINAL_STAGES, LeadOut
from app.services import events


def _agency_scope() -> Select:
    """Leads joined to their owning agent — the hop every tenancy check makes."""
    return select(Lead).join(Agent, Agent.id == Lead.agent_id)


async def get_lead(session: AsyncSession, lead_id: uuid.UUID, agency_id: uuid.UUID) -> Lead:
    lead = (
        await session.execute(
            _agency_scope().where(Lead.id == lead_id, Agent.agency_id == agency_id)
        )
    ).scalar_one_or_none()
    if lead is None:
        # 404 not 403 — do not confirm that a lead exists in another agency.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead


async def create_or_get_lead(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    listing_id: uuid.UUID,
    source_channel: str,
    message: str | None,
    agency_id: uuid.UUID,
    actor: Agent | None = None,
) -> tuple[Lead, bool]:
    """HU-01 CA3. Returns (lead, created).

    Dedup is the database's UNIQUE (client_id, listing_id) constraint, reached via
    ON CONFLICT DO NOTHING. Checking in Python first would race: two concurrent
    requests both see nothing and both insert, and one gets an IntegrityError.
    Here the loser of the race simply reads the winner's row.

    `actor` is recorded as `changed_by` on the opening transition, so a lead a
    service account created is attributable to it. Ownership is unaffected: the
    listing's agent owns the lead whoever created it.
    """
    listing = (
        await session.execute(
            select(Listing)
            .join(Agent, Agent.id == Listing.agent_id)
            .where(Listing.id == listing_id, Agent.agency_id == agency_id)
        )
    ).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found")

    if not await session.scalar(select(exists().where(Client.id == client_id))):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    stmt = (
        insert(Lead)
        .values(
            client_id=client_id,
            listing_id=listing_id,
            agent_id=listing.agent_id,       # the listing's agent owns the lead
            source_channel=source_channel,
        )
        .on_conflict_do_nothing(index_elements=[Lead.client_id, Lead.listing_id])
        .returning(Lead.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()

    if new_id is None:
        # Conflict: the thread already exists. Return it rather than erroring.
        existing = (
            await session.execute(
                select(Lead).where(
                    Lead.client_id == client_id, Lead.listing_id == listing_id
                )
            )
        ).scalar_one()
        await session.commit()
        return existing, False

    # Opening transition. The trigger sets lead.current_stage from this.
    session.add(
        LeadStageTransition(
            lead_id=new_id, from_stage=None, to_stage="INTERESTED",
            changed_by=actor.id if actor else None,
        )
    )
    if message:
        session.add(
            Interaction(
                lead_id=new_id,
                direction="INBOUND",
                channel=source_channel,
                type="MESSAGE",
                body=message,
                # A bot relaying the client's message is still the bot writing
                # the row; a human's inbound stays unattributed, as before.
                created_by=actor.id if actor and actor.is_bot else None,
            )
        )
    # Outbox row in the same transaction: the event exists iff the lead does.
    events.emit(
        session,
        event_type="lead.created",
        aggregate_type="lead",
        aggregate_id=new_id,
        agency_id=agency_id,
        payload={"listing_id": str(listing_id), "client_id": str(client_id),
                 "agent_id": str(listing.agent_id)},
    )
    await session.commit()

    lead = (await session.execute(select(Lead).where(Lead.id == new_id))).scalar_one()
    return lead, True


_PREVIEW_CHARS = 140


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(get_settings().app_timezone))


async def list_leads(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    stage: str | None = None,
    agent_id: uuid.UUID | None = None,
    listing_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    active: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """The board: one card per lead, newest activity first (HU-06).

    Client, listing and last interaction come back in the same query, so a
    board of 200 cards is one round trip, not 401. `created_from`/`created_to`
    are calendar days in APP_TIMEZONE, both inclusive.
    """
    last = (
        select(
            Interaction.occurred_at, Interaction.direction, Interaction.type,
            func.left(Interaction.body, _PREVIEW_CHARS).label("body"),
        )
        .where(Interaction.lead_id == Lead.id)
        .order_by(Interaction.occurred_at.desc())
        .limit(1)
        .lateral("last_interaction")
    )
    q = (
        _agency_scope()
        .join(Client, Client.id == Lead.client_id)
        .join(Person, Person.id == Client.person_id)
        .join(Listing, Listing.id == Lead.listing_id)
        .join(Property, Property.id == Listing.property_id)
        .outerjoin(last, true())
        .add_columns(
            Person.full_name, Property.address, Property.neighborhood,
            Listing.operation_type, Listing.asking_price,
            last.c.occurred_at, last.c.direction, last.c.type, last.c.body,
        )
        .where(Agent.agency_id == agency_id)
    )
    if stage:
        q = q.where(Lead.current_stage == stage)
    if active:
        # HU-09 AC2: a closed lead leaves the working board, never the table.
        q = q.where(Lead.current_stage.not_in(TERMINAL_STAGES))
    if agent_id:
        q = q.where(Lead.agent_id == agent_id)
    if listing_id:
        q = q.where(Lead.listing_id == listing_id)
    if property_id:
        q = q.where(Listing.property_id == property_id)
    if client_id:
        # How the bot finds a returning client's threads in this agency.
        q = q.where(Lead.client_id == client_id)
    if created_from:
        q = q.where(Lead.created_at >= _local_midnight(created_from))
    if created_to:
        q = q.where(Lead.created_at < _local_midnight(created_to + timedelta(days=1)))
    q = q.order_by(Lead.updated_at.desc()).limit(limit).offset(offset)

    cards = []
    for lead, name, address, hood, op, price, li_at, li_dir, li_type, li_body in (
        await session.execute(q)
    ).all():
        cards.append({
            **LeadOut.model_validate(lead).model_dump(),
            "client_name": name,
            "listing_address": address,
            "neighborhood": hood,
            "operation_type": op,
            "asking_price": price,
            "last_interaction": None if li_at is None else {
                "occurred_at": li_at, "direction": li_dir, "type": li_type, "body": li_body,
            },
        })
    return cards


def insert_transition(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    from_stage: str | None,
    to_stage: str,
    actor: Agent,
    agency_id: uuid.UUID,
) -> LeadStageTransition:
    """Stage one transition and its event in the caller's transaction; no commit.

    The row is the truth and the trigger updates lead.current_stage. The caller
    has already validated the edge: add_transition against ALLOWED_TRANSITIONS,
    the calendar (services/appointment) by only ever taking a forward edge the
    funnel allows.
    """
    transition = LeadStageTransition(
        lead_id=lead_id, from_stage=from_stage, to_stage=to_stage, changed_by=actor.id
    )
    session.add(transition)
    events.emit(
        session,
        event_type="lead.stage_changed",
        aggregate_type="lead",
        aggregate_id=lead_id,
        agency_id=agency_id,
        payload={"from": from_stage, "to": to_stage},
    )
    return transition


async def add_transition(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    to_stage: str,
    lost_reason: str | None,
    note: str | None,
    agent: Agent,
) -> LeadStageTransition:
    """Validate the edge, insert the transition, let the trigger update the cache.

    LOST additionally requires a lead_lost_detail row. The schema cannot express
    that dependency, so it is written here in the same transaction: either both
    land or neither does. Closing a lead (WON or LOST) also cancels its open
    visits in that transaction, so the agent's slot frees up.
    """
    lead = await get_lead(session, lead_id, agent.agency_id)
    current = lead.current_stage

    if current in TERMINAL_STAGES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Lead is already in terminal stage {current} and cannot be moved",
        )
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if to_stage not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Illegal transition {current} -> {to_stage}. "
            f"Allowed from {current}: {sorted(allowed) or 'none (terminal)'}",
        )

    transition = insert_transition(
        session, lead_id=lead_id, from_stage=current, to_stage=to_stage,
        actor=agent, agency_id=agent.agency_id,
    )

    if to_stage == "LOST":
        reason_id = await session.scalar(
            select(LostReason.id).where(LostReason.code == lost_reason)
        )
        if reason_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Unknown lost_reason {lost_reason!r}")
        session.add(
            LeadLostDetail(lead_id=lead_id, lost_reason_id=reason_id, free_text=note)
        )

    # LOST always gets a timeline line, so the reason stays readable in the
    # lead's history (HU-09 AC2); other stages only when there is a note.
    # STATUS_CHANGE is never a response, so neither stops the response clock.
    body = note
    if to_stage == "LOST":
        body = f"Lost: {lost_reason}" + (f" — {note}" if note else "")
    if body:
        session.add(
            Interaction(
                lead_id=lead_id, direction="OUTBOUND", channel="IN_APP",
                type="STATUS_CHANGE", body=body, created_by=agent.id,
            )
        )

    if to_stage in TERMINAL_STAGES:
        from app.services.appointment import cancel_open_visits  # lazy: import cycle

        await cancel_open_visits(session, lead_id=lead_id, actor=agent, closed_as=to_stage)

    await session.commit()
    await session.refresh(transition)
    return transition


async def list_transitions(
    session: AsyncSession, *, lead_id: uuid.UUID, agency_id: uuid.UUID
) -> list[LeadStageTransition]:
    await get_lead(session, lead_id, agency_id)
    return list(
        (
            await session.execute(
                select(LeadStageTransition)
                .where(LeadStageTransition.lead_id == lead_id)
                .order_by(LeadStageTransition.changed_at)
            )
        ).scalars().all()
    )


async def reassign(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    to_agent_id: uuid.UUID,
    actor: Agent,
) -> Lead:
    """Move the lead AND write the audit row. Doing only one of the two is the
    bug the seeder had: an audit trail that disagrees with the lead itself."""
    lead = await get_lead(session, lead_id, actor.agency_id)

    target = (
        await session.execute(
            select(Agent).where(Agent.id == to_agent_id, Agent.agency_id == actor.agency_id)
        )
    ).scalar_one_or_none()
    if target is None:
        # Either it does not exist or it belongs to another agency; same answer.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target agent not found in this agency")
    if not target.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Target agent is deactivated")
    if target.is_bot:
        # A bot never owns a lead: it would surface in the per-agent leaderboard
        # and nobody would be responsible for the client.
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot assign a lead to an AI agent")
    if target.id == lead.agent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Lead is already assigned to that agent")

    previous_agent_id = lead.agent_id
    lead.agent_id = target.id
    lead.updated_at = datetime.now(timezone.utc)
    session.add(
        AssignmentAudit(
            lead_id=lead_id,
            from_agent_id=previous_agent_id,
            to_agent_id=target.id,
            reassigned_by=actor.id,
        )
    )
    await session.commit()
    await session.refresh(lead)
    return lead


# ------------------------------------------------------------- interactions

async def list_interactions(
    session: AsyncSession, *, lead_id: uuid.UUID, agency_id: uuid.UUID
) -> list[Interaction]:
    await get_lead(session, lead_id, agency_id)
    return list(
        (
            await session.execute(
                select(Interaction)
                .where(Interaction.lead_id == lead_id)
                .order_by(Interaction.occurred_at)
            )
        ).scalars().all()
    )


async def add_interaction(
    session: AsyncSession, *, lead_id: uuid.UUID, agent: Agent, data
) -> Interaction:
    await get_lead(session, lead_id, agent.agency_id)
    row = Interaction(
        lead_id=lead_id,
        direction=data.direction,
        channel=data.channel,
        type=data.type,
        body=data.body,
        # OUTBOUND is attributed so the response-time metric knows who replied;
        # a bot is attributed on everything so its activity is auditable and
        # the views can exclude it.
        created_by=agent.id if data.direction == "OUTBOUND" or agent.is_bot else None,
        **({"occurred_at": data.occurred_at} if data.occurred_at else {}),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# --------------------------------------------------------------- at-risk

async def leads_at_risk(
    session: AsyncSession, *, agency_id: uuid.UUID, hours: int, limit: int = 100
) -> list[Lead]:
    """Non-terminal leads with no OUTBOUND interaction in `hours`.

    Same predicate the inactivity job uses, exposed so an agent can see the
    queue without waiting for the sweep.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    terminal = select(LeadStage.code).where(LeadStage.is_terminal.is_(True))

    # Same definition of "response" as the views and the sweep (007): an
    # OUTBOUND MESSAGE or CALL written by a human. Notes, stage-change notes
    # and anything an AI agent wrote do not count as contact.
    author = aliased(Agent)
    last_outbound = (
        select(func.max(Interaction.occurred_at))
        .select_from(Interaction)
        .outerjoin(author, author.id == Interaction.created_by)
        .where(
            Interaction.lead_id == Lead.id,
            Interaction.direction == "OUTBOUND",
            Interaction.type.in_(RESPONSE_TYPES),
            or_(author.role.is_(None), author.role != "AI_AGENT"),
        )
        .correlate(Lead)
        .scalar_subquery()
    )

    q = (
        _agency_scope()
        .where(
            Agent.agency_id == agency_id,
            Lead.current_stage.not_in(terminal),
            or_(last_outbound.is_(None), last_outbound < cutoff),
            Lead.created_at < cutoff,
        )
        .order_by(Lead.created_at)
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


# ------------------------------------------------------------------ tasks

async def list_tasks(
    session: AsyncSession, *, lead_id: uuid.UUID, agency_id: uuid.UUID
) -> list[FollowUpTask]:
    await get_lead(session, lead_id, agency_id)
    return list(
        (
            await session.execute(
                select(FollowUpTask)
                .where(FollowUpTask.lead_id == lead_id)
                .order_by(FollowUpTask.due_at)
            )
        ).scalars().all()
    )


async def create_task(
    session: AsyncSession, *, lead_id: uuid.UUID, agent: Agent, due_at, note: str | None
) -> FollowUpTask:
    lead = await get_lead(session, lead_id, agent.agency_id)
    task = FollowUpTask(lead_id=lead_id, agent_id=lead.agent_id, due_at=due_at, note=note)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def patch_task(
    session: AsyncSession, *, lead_id: uuid.UUID, task_id: uuid.UUID, agency_id: uuid.UUID, data
) -> FollowUpTask:
    await get_lead(session, lead_id, agency_id)
    task = (
        await session.execute(
            select(FollowUpTask).where(
                FollowUpTask.id == task_id, FollowUpTask.lead_id == lead_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    for field in ("status", "due_at", "note"):
        value = getattr(data, field)
        if value is not None:
            setattr(task, field, value)
    await session.commit()
    await session.refresh(task)
    return task

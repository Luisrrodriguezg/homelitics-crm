"""The in-app inbox (014).

A message is written when the AI agent or a TEAM_ADMIN hands an agent work: a
lead assigned to them (created by the bot, or reassigned by an admin) or a visit
booked on their calendar. Nobody gets a message about something they did
themselves, and an ordinary agent's own actions notify no one.

`notify` stages the row in the **caller's** transaction, like `events.emit`: the
message exists iff the lead or visit does. Rows hold ids only; names and
addresses are joined in by `list_notifications`, so scrubbing pii.person still
erases a person everywhere (CLAUDE.md rule 1).

Every read filters on agency_id, and the inbox routes also on the recipient.
The admin monitor reads the same rows agency-wide and never marks them read.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Agent, Appointment, Client, Lead, Listing, Notification, Person, Property,
)
# A module import, not `from ... import`: lead.py imports this module back.
from app.services import lead as _lead

PAGE_SIZE = 20


def notify(
    session: AsyncSession,
    *,
    type: str,
    agency_id: uuid.UUID,
    recipient_id: uuid.UUID,
    actor: Agent | None,
    lead_id: uuid.UUID,
    appointment_id: uuid.UUID | None = None,
    data: dict | None = None,
) -> Notification | None:
    """Stage a message for `recipient_id` if the bot or an admin caused it.

    Returns None (and writes nothing) when the actor is the recipient, an
    ordinary agent, or the system.
    """
    if actor is None or actor.id == recipient_id:
        return None
    if not (actor.is_bot or actor.role == "TEAM_ADMIN"):
        return None
    row = Notification(
        type=type,
        agency_id=agency_id,
        recipient_agent_id=recipient_id,
        actor_agent_id=actor.id,
        lead_id=lead_id,
        appointment_id=appointment_id,
        data=data or {},
    )
    session.add(row)
    return row


async def list_notifications(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    recipient_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    by_bot: bool | None = None,
    type: str | None = None,
    read: bool | None = None,
    lead_id: uuid.UUID | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    before: int | None = None,
    limit: int = PAGE_SIZE,
) -> dict:
    """Newest first, `limit` per page. `unread_count` covers every page of the
    same filters (ignoring `read` and `before`), so with no filters it is the
    badge. `next_before` is the cursor for the next page, None on the last."""
    Recipient, Actor = aliased(Agent), aliased(Agent)
    RecipientP, ActorP, ClientP = aliased(Person), aliased(Person), aliased(Person)

    conds = [Notification.agency_id == agency_id]
    if recipient_id:
        conds.append(Notification.recipient_agent_id == recipient_id)
    if actor_id:
        conds.append(Notification.actor_agent_id == actor_id)
    if by_bot is not None:
        bots = select(Agent.id).where(Agent.role == "AI_AGENT")
        conds.append(Notification.actor_agent_id.in_(bots) if by_bot
                     else Notification.actor_agent_id.not_in(bots))
    if type:
        conds.append(Notification.type == type)
    if lead_id:
        conds.append(Notification.lead_id == lead_id)
    if created_from:
        conds.append(Notification.created_at >= _lead._local_midnight(created_from))
    if created_to:
        conds.append(Notification.created_at < _lead._local_midnight(created_to + timedelta(days=1)))

    unread_count = await session.scalar(
        select(func.count()).select_from(Notification)
        .where(*conds, Notification.read_at.is_(None))
    )

    page = list(conds)
    if read is not None:
        page.append(Notification.read_at.is_not(None) if read else Notification.read_at.is_(None))
    if before:
        page.append(Notification.id < before)

    q = (
        select(
            Notification,
            RecipientP.full_name, ActorP.full_name, Actor.role,
            ClientP.full_name, Property.address, Property.neighborhood,
            Appointment.scheduled_at, Appointment.duration_min, Appointment.status,
        )
        .join(Recipient, Recipient.id == Notification.recipient_agent_id)
        .join(RecipientP, RecipientP.id == Recipient.person_id)
        .outerjoin(Actor, Actor.id == Notification.actor_agent_id)
        .outerjoin(ActorP, ActorP.id == Actor.person_id)
        .join(Lead, Lead.id == Notification.lead_id)
        .join(Client, Client.id == Lead.client_id)
        .join(ClientP, ClientP.id == Client.person_id)
        .join(Listing, Listing.id == Lead.listing_id)
        .join(Property, Property.id == Listing.property_id)
        .outerjoin(Appointment, Appointment.id == Notification.appointment_id)
        .where(*page)
        .order_by(Notification.id.desc())
        .limit(limit)
    )
    items = []
    for (n, recipient_name, actor_name, actor_role, client_name, address, hood,
         visit_at, visit_min, visit_status) in (await session.execute(q)).all():
        items.append({
            "id": n.id,
            "type": n.type,
            "read": n.read_at is not None,
            "read_at": n.read_at,
            "created_at": n.created_at,
            "recipient_agent_id": n.recipient_agent_id,
            "recipient_name": recipient_name,
            "actor_agent_id": n.actor_agent_id,
            "actor_name": actor_name,
            "actor_role": actor_role,
            "lead_id": n.lead_id,
            "client_name": client_name,
            "listing_address": address,
            "neighborhood": hood,
            "assigned_via": n.data.get("via"),
            "from_agent_id": n.data.get("from_agent_id"),
            "appointment_id": n.appointment_id,
            # The visit as it is now (it may have been confirmed or moved since),
            # plus the status it was booked with.
            "visit_scheduled_at": visit_at,
            "visit_duration_min": visit_min,
            "visit_status": visit_status,
            "booked_status": n.data.get("status"),
        })
    return {
        "items": items,
        "unread_count": unread_count,
        "next_before": items[-1]["id"] if len(items) == limit else None,
    }


async def mark_read(
    session: AsyncSession, *, notification_id: int, agent: Agent
) -> Notification | None:
    """Mark one of the caller's own messages read. Already read keeps its
    original read_at. None if it is not the caller's (the router 404s)."""
    row = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.recipient_agent_id == agent.id,
                Notification.agency_id == agent.agency_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await session.commit()
    return row


async def mark_all_read(session: AsyncSession, *, agent: Agent) -> int:
    """Mark every unread message of the caller read. Returns how many changed."""
    result = await session.execute(
        update(Notification)
        .where(
            Notification.recipient_agent_id == agent.id,
            Notification.agency_id == agent.agency_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    await session.commit()
    return result.rowcount

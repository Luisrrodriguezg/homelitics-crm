"""Agent directory — who a lead can be reassigned to."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent


async def list_agents(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    active: bool | None = None,
    role: str | None = None,
    include_bots: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Agent]:
    q = select(Agent).where(Agent.agency_id == agency_id)
    if not include_bots:
        q = q.where(Agent.role != "AI_AGENT")
    if active is not None:
        q = q.where(Agent.active == active)
    if role:
        q = q.where(Agent.role == role)
    q = q.order_by(Agent.created_at, Agent.id).limit(limit).offset(offset)
    return list((await session.execute(q)).scalars().all())

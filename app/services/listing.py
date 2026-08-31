"""Listings and the property-view event stream."""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Agent, Client, Listing, PropertyView


def _scoped(agency_id: uuid.UUID):
    return (
        select(Listing)
        .options(joinedload(Listing.property))
        .join(Agent, Agent.id == Listing.agent_id)
        .where(Agent.agency_id == agency_id)
    )


async def list_listings(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    status_filter: str | None = None,
    operation_type: str | None = None,
    city: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Listing]:
    q = _scoped(agency_id)
    if status_filter:
        q = q.where(Listing.status == status_filter)
    if operation_type:
        q = q.where(Listing.operation_type == operation_type)
    if city:
        # .has() emits EXISTS; joining property again would collide with the joinedload.
        q = q.where(Listing.property.has(city=city))
    q = q.order_by(Listing.published_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(q)).unique().scalars().all())


async def get_listing(
    session: AsyncSession, *, listing_id: uuid.UUID, agency_id: uuid.UUID
) -> Listing:
    listing = (
        await session.execute(_scoped(agency_id).where(Listing.id == listing_id))
    ).unique().scalar_one_or_none()
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found")
    return listing


async def record_view(
    session: AsyncSession,
    *,
    listing_id: uuid.UUID,
    agency_id: uuid.UUID,
    session_id: str,
    client_id: uuid.UUID | None,
) -> PropertyView:
    """Append-only. Feeds analytics.listing_performance.views."""
    await get_listing(session, listing_id=listing_id, agency_id=agency_id)

    if client_id is not None:
        exists_client = await session.scalar(select(Client.id).where(Client.id == client_id))
        if exists_client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    view = PropertyView(listing_id=listing_id, client_id=client_id, session_id=session_id)
    session.add(view)
    await session.commit()
    await session.refresh(view)
    return view

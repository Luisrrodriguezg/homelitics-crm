"""Listing catalogue and the view event stream."""
import uuid

from fastapi import APIRouter, Query, status

from app.deps import CurrentAgent, DbSession
from app.schemas import ListingOut, ListingStatus, Message, OperationType, ViewCreate
from app.services import listing as svc

router = APIRouter(prefix="/listings", tags=["listings"])


def _flatten(listing) -> dict:
    """Listing + its property, flattened for the response model."""
    p = listing.property
    return {
        "id": listing.id,
        "property_id": listing.property_id,
        "agent_id": listing.agent_id,
        "operation_type": listing.operation_type,
        "asking_price": listing.asking_price,
        "status": listing.status,
        "published_at": listing.published_at,
        "city": p.city if p else None,
        "neighborhood": p.neighborhood if p else None,
        "address": p.address if p else None,
        "property_type": p.property_type if p else None,
        "area_m2": p.area_m2 if p else None,
        "bedrooms": p.bedrooms if p else None,
        "bathrooms": p.bathrooms if p else None,
    }


@router.get(
    "",
    response_model=list[ListingOut],
    summary="Listing catalogue",
    description="Listings belonging to the caller's agency, newest first.",
)
async def list_listings(
    agent: CurrentAgent,
    session: DbSession,
    status_filter: ListingStatus | None = Query(None, alias="status"),
    operation_type: OperationType | None = Query(None),
    city: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = await svc.list_listings(
        session, agency_id=agent.agency_id, status_filter=status_filter,
        operation_type=operation_type, city=city, limit=limit, offset=offset,
    )
    return [_flatten(r) for r in rows]


@router.get(
    "/{listing_id}",
    response_model=ListingOut,
    summary="One listing",
    responses={404: {"model": Message, "description": "Not found, or not in your agency"}},
)
async def get_listing(listing_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return _flatten(
        await svc.get_listing(session, listing_id=listing_id, agency_id=agent.agency_id)
    )


@router.post(
    "/{listing_id}/views",
    status_code=status.HTTP_201_CREATED,
    summary="Record a listing page view",
    description="Append-only event feeding `analytics.listing_performance.views`. "
                "`client_id` is optional — anonymous traffic still counts.",
    responses={404: {"model": Message, "description": "Listing or client not found"}},
)
async def record_view(
    listing_id: uuid.UUID, payload: ViewCreate, agent: CurrentAgent, session: DbSession
):
    view = await svc.record_view(
        session, listing_id=listing_id, agency_id=agent.agency_id,
        session_id=payload.session_id, client_id=payload.client_id,
    )
    return {"id": view.id, "listing_id": view.listing_id, "viewed_at": view.viewed_at}

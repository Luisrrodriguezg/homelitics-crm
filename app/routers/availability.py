"""HU-05: an agent publishes when they are reachable, and the derived free slots.

Weekly rules + ad-hoc time off are stored; `GET /agents/{id}/slots` turns them
into a bookable 30-minute grid (see app/services/availability.compute_slots).
Everything is agency-scoped through the same `core.agent` join as the rest of
the API — an agent in another agency is a 404.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Response, status

from app.deps import CurrentAgent, DbSession
from app.schemas import (
    AvailabilityCreate, AvailabilityOut, AvailabilityPatch, Message,
    SlotsOut, TimeOffCreate, TimeOffOut,
)
from app.services import availability as svc
from app.services.availability import SLOT_MINUTES

router = APIRouter(prefix="/agents", tags=["availability"])


@router.get("/{agent_id}/availability", response_model=list[AvailabilityOut],
            summary="Weekly availability rules")
async def list_availability(agent_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_availability(session, agent_id=agent_id, agency_id=agent.agency_id)


@router.post("/{agent_id}/availability", response_model=AvailabilityOut,
             status_code=status.HTTP_201_CREATED, summary="Add a weekly availability block",
             responses={404: {"model": Message, "description": "Agent not in your agency"}})
async def add_availability(
    agent_id: uuid.UUID, payload: AvailabilityCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_availability(
        session, agent_id=agent_id, agency_id=agent.agency_id, data=payload
    )


@router.patch("/{agent_id}/availability/{rule_id}", response_model=AvailabilityOut,
              summary="Edit a weekly availability block",
              responses={404: {"model": Message, "description": "Rule or agent not found"}})
async def patch_availability(
    agent_id: uuid.UUID, rule_id: uuid.UUID, payload: AvailabilityPatch,
    agent: CurrentAgent, session: DbSession,
):
    return await svc.patch_availability(
        session, agent_id=agent_id, rule_id=rule_id, agency_id=agent.agency_id, data=payload
    )


@router.delete("/{agent_id}/availability/{rule_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove a weekly availability block")
async def delete_availability(
    agent_id: uuid.UUID, rule_id: uuid.UUID, agent: CurrentAgent, session: DbSession
):
    await svc.delete_availability(
        session, agent_id=agent_id, rule_id=rule_id, agency_id=agent.agency_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/time-off", response_model=list[TimeOffOut], summary="Time off")
async def list_time_off(agent_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_time_off(session, agent_id=agent_id, agency_id=agent.agency_id)


@router.post("/{agent_id}/time-off", response_model=TimeOffOut,
             status_code=status.HTTP_201_CREATED, summary="Book time off")
async def add_time_off(
    agent_id: uuid.UUID, payload: TimeOffCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_time_off(
        session, agent_id=agent_id, agency_id=agent.agency_id, data=payload
    )


@router.delete("/{agent_id}/time-off/{off_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Cancel time off")
async def delete_time_off(
    agent_id: uuid.UUID, off_id: uuid.UUID, agent: CurrentAgent, session: DbSession
):
    await svc.delete_time_off(
        session, agent_id=agent_id, off_id=off_id, agency_id=agent.agency_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/slots", response_model=SlotsOut,
            summary="Free 30-minute slots",
            description="Weekly rules expanded over the window, minus time off and "
                        "minus appointments that still occupy the calendar.")
async def get_slots(
    agent_id: uuid.UUID,
    agent: CurrentAgent,
    session: DbSession,
    from_: datetime = Query(alias="from", description="window start (ISO 8601)"),
    to: datetime = Query(description="window end (ISO 8601)"),
):
    slots = await svc.compute_slots(
        session, agent_id=agent_id, agency_id=agent.agency_id, start=from_, end=to
    )
    return SlotsOut(agent_id=agent_id, slot_minutes=SLOT_MINUTES, slots=slots)

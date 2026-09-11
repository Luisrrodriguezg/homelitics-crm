"""HU-05: an agent publishes when they are reachable, and the derived free slots.

Weekly rules + ad-hoc time off are stored; `GET /agents/{id}/slots` turns them
into a bookable 30-minute grid (see app/services/availability.compute_slots).
Everything is agency-scoped through the same `core.agent` join as the rest of
the API — an agent in another agency is a 404.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from app.deps import CurrentAgent, DbSession, require_scope
from app.schemas import (
    AvailabilityCreate, AvailabilityOut, AvailabilityPatch, Message,
    SlotsOut, TimeOffCreate, TimeOffOut,
)
from app.services import availability as svc
from app.services.availability import SLOT_MINUTES

router = APIRouter(prefix="/agents", tags=["availability"])

# One scope for the whole calendar: a bot editing when humans are reachable is
# not something the default grant includes.
_WRITE = [Depends(require_scope("availability:write"))]


@router.get("/{agent_id}/availability", response_model=list[AvailabilityOut],
            summary="Weekly availability rules")
async def list_availability(agent_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_availability(session, agent_id=agent_id, agency_id=agent.agency_id)


@router.post("/{agent_id}/availability", response_model=AvailabilityOut,
             status_code=status.HTTP_201_CREATED, summary="Add a weekly availability block",
             responses={404: {"model": Message, "description": "Agent not in your agency"}},
             dependencies=_WRITE)
async def add_availability(
    agent_id: uuid.UUID, payload: AvailabilityCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_availability(
        session, agent_id=agent_id, agency_id=agent.agency_id, data=payload
    )


@router.patch("/{agent_id}/availability/{rule_id}", response_model=AvailabilityOut,
              summary="Edit a weekly availability block",
              responses={404: {"model": Message, "description": "Rule or agent not found"}},
              dependencies=_WRITE)
async def patch_availability(
    agent_id: uuid.UUID, rule_id: uuid.UUID, payload: AvailabilityPatch,
    agent: CurrentAgent, session: DbSession,
):
    return await svc.patch_availability(
        session, agent_id=agent_id, rule_id=rule_id, agency_id=agent.agency_id, data=payload
    )


@router.delete("/{agent_id}/availability/{rule_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove a weekly availability block", dependencies=_WRITE)
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
             status_code=status.HTTP_201_CREATED, summary="Book time off",
             description="Refused with **409** if it overlaps a visit still on the "
                         "calendar — move or cancel those first.",
             responses={409: {"model": Message, "description": "Overlaps a booked visit"}},
             dependencies=_WRITE)
async def add_time_off(
    agent_id: uuid.UUID, payload: TimeOffCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_time_off(
        session, agent_id=agent_id, agency_id=agent.agency_id, data=payload
    )


@router.delete("/{agent_id}/time-off/{off_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Cancel time off",
               description="Manual time off only; an imported block (`source: ICS`) is "
                           "**409** — remove it in the agent's own calendar.",
               dependencies=_WRITE)
async def delete_time_off(
    agent_id: uuid.UUID, off_id: uuid.UUID, agent: CurrentAgent, session: DbSession
):
    await svc.delete_time_off(
        session, agent_id=agent_id, off_id=off_id, agency_id=agent.agency_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/slots", response_model=SlotsOut,
            summary="Free slots for a visit",
            description="Weekly rules expanded over the window on a 30-minute grid, "
                        "minus time off (manual and imported from the agent's own "
                        "calendar) and minus visits that still occupy the calendar. "
                        "Every start returned leaves room for a visit of `duration_min` "
                        "(default 30 — pass 60 for a standard visit) and is in the "
                        "future. This is exactly what an AI agent may book.")
async def get_slots(
    agent_id: uuid.UUID,
    agent: CurrentAgent,
    session: DbSession,
    from_: datetime = Query(alias="from", description="window start (ISO 8601)"),
    to: datetime = Query(description="window end (ISO 8601)"),
    duration_min: int = Query(SLOT_MINUTES, ge=15, le=480,
                              description="length of the visit to fit, in minutes"),
):
    slots = await svc.free_slots(
        session, agent_id=agent_id, agency_id=agent.agency_id, start=from_, end=to,
        duration_min=duration_min,
    )
    return SlotsOut(agent_id=agent_id, slot_minutes=SLOT_MINUTES,
                    duration_min=duration_min, slots=slots)

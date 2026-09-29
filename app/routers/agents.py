"""Agent directory: the list a reassignment picker is built from."""
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.deps import CurrentAgent, DbSession
from app.schemas import AgentListItem, Message
from app.services import agent as svc

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get(
    "",
    response_model=list[AgentListItem],
    summary="Agents in your agency",
    description=(
        "The agents of the caller's agency, oldest first. AI agents are left out "
        "unless `include_bots=true` — they cannot own a lead, so they are never a "
        "reassignment target. Use `id` as `to_agent_id` in `POST /leads/{id}/reassign`.\n\n"
        "`agency_id` is optional and exists for symmetry with other filters: it must "
        "equal the caller's own agency, anything else is **404** (tenancy is never "
        "widened by a query parameter). No contact details are returned."
    ),
    responses={404: {"model": Message, "description": "agency_id is not your agency"}},
)
async def list_agents(
    agent: CurrentAgent,
    session: DbSession,
    agency_id: uuid.UUID | None = Query(default=None),
    active: bool | None = Query(default=None, description="Only active / only deactivated"),
    role: Literal["AGENT", "TEAM_ADMIN", "AI_AGENT"] | None = Query(default=None),
    include_bots: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if agency_id is not None and agency_id != agent.agency_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agency not found")
    rows = await svc.list_agents(
        session,
        agency_id=agent.agency_id,
        active=active,
        role=role,
        include_bots=include_bots or role == "AI_AGENT",
        limit=limit,
        offset=offset,
    )
    return [
        AgentListItem(
            id=a.id, agency_id=a.agency_id, role=a.role, active=a.active,
            full_name=a.person.full_name if a.person else None,
        )
        for a in rows
    ]

"""Lead board, funnel transitions, timeline, tasks, reassignment."""
import uuid

from fastapi import APIRouter, Query, Response, status

from app.config import get_settings
from app.deps import CurrentAgent, DbSession, TeamAdmin
from app.schemas import (
    InteractionCreate, InteractionOut, LeadCreate, LeadOut, Message,
    ReassignRequest, Stage, TaskCreate, TaskOut, TaskPatch, TransitionCreate,
    TransitionOut,
)
from app.services import lead as svc

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post(
    "",
    response_model=LeadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a lead, or return the existing thread",
    description=(
        "HU-01 CA3. A (client, listing) pair identifies one conversation. Posting the "
        "same pair twice does **not** create a duplicate: the second call returns the "
        "existing lead with **200** instead of **201**.\n\n"
        "Dedup is enforced by the database's `UNIQUE (client_id, listing_id)` via "
        "`ON CONFLICT DO NOTHING`, so it is race-free — two simultaneous requests "
        "cannot both win."
    ),
    responses={
        200: {"description": "Lead already existed; the existing thread is returned"},
        201: {"description": "New lead created"},
        404: {"description": "Listing not in your agency, or client does not exist"},
    },
)
async def create_lead(
    payload: LeadCreate, agent: CurrentAgent, session: DbSession, response: Response
):
    lead, created = await svc.create_or_get_lead(
        session,
        client_id=payload.client_id,
        listing_id=payload.listing_id,
        source_channel=payload.source_channel,
        message=payload.message,
        agency_id=agent.agency_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return lead


@router.get(
    "",
    response_model=list[LeadOut],
    summary="Lead board",
    description="All leads in the caller's agency, newest activity first. Filterable "
                "by stage, owning agent and listing.",
)
async def list_leads(
    agent: CurrentAgent,
    session: DbSession,
    stage: Stage | None = Query(None, description="Filter by current funnel stage"),
    agent_id: uuid.UUID | None = Query(None, description="Filter by owning agent"),
    listing_id: uuid.UUID | None = Query(None, description="Filter by listing"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await svc.list_leads(
        session, agency_id=agent.agency_id, stage=stage, agent_id=agent_id,
        listing_id=listing_id, limit=limit, offset=offset,
    )


@router.get(
    "/at-risk",
    response_model=list[LeadOut],
    summary="Leads going cold",
    description="Non-terminal leads with no OUTBOUND interaction inside the inactivity "
                "window. Same predicate the hourly sweep uses, so you can see the queue "
                "without waiting for it to run.",
)
async def leads_at_risk(
    agent: CurrentAgent,
    session: DbSession,
    hours: int | None = Query(None, ge=1, le=8760,
                              description="Override the configured INACTIVITY_HOURS"),
    limit: int = Query(100, ge=1, le=500),
):
    window = hours or get_settings().inactivity_hours
    return await svc.leads_at_risk(
        session, agency_id=agent.agency_id, hours=window, limit=limit
    )


@router.get(
    "/{lead_id}",
    response_model=LeadOut,
    summary="One lead",
    responses={404: {"model": Message, "description": "Not found, or not in your agency"}},
)
async def get_lead(lead_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.get_lead(session, lead_id, agent.agency_id)


# ----------------------------------------------------------------- funnel

@router.get(
    "/{lead_id}/transitions",
    response_model=list[TransitionOut],
    summary="Stage history",
    description="The append-only transition log. This is the source of truth; "
                "`lead.current_stage` is a trigger-maintained cache of it.",
)
async def list_transitions(lead_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_transitions(session, lead_id=lead_id, agency_id=agent.agency_id)


@router.post(
    "/{lead_id}/transitions",
    response_model=TransitionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Move the lead to a new stage",
    description=(
        "Validates the edge against the funnel, inserts the transition and lets the "
        "database trigger update `lead.current_stage`.\n\n"
        "Legal edges: INTERESTED→VISIT_SCHEDULED→VISITED→NEGOTIATING→WON. Any "
        "non-terminal stage may go to LOST. WON and LOST are terminal.\n\n"
        "Moving to **LOST requires `lost_reason`**; the reason row is written in the "
        "same transaction, an invariant the schema itself cannot express."
    ),
    responses={
        409: {"model": Message, "description": "Illegal or terminal transition"},
        422: {"model": Message, "description": "LOST without a lost_reason, or unknown reason"},
    },
)
async def add_transition(
    lead_id: uuid.UUID, payload: TransitionCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_transition(
        session, lead_id=lead_id, to_stage=payload.to_stage,
        lost_reason=payload.lost_reason, note=payload.note, agent=agent,
    )


@router.post(
    "/{lead_id}/reassign",
    response_model=LeadOut,
    summary="Hand the lead to another agent (TEAM_ADMIN only)",
    description="Updates `lead.agent_id` **and** writes `assignment_audit` in one "
                "transaction. The target must be an active agent in the same agency.",
    responses={
        403: {"model": Message, "description": "Caller is not a TEAM_ADMIN"},
        404: {"model": Message, "description": "Target agent is not in your agency"},
        409: {"model": Message, "description": "Target is deactivated, or already owns the lead"},
    },
)
async def reassign(
    lead_id: uuid.UUID, payload: ReassignRequest, admin: TeamAdmin, session: DbSession
):
    return await svc.reassign(
        session, lead_id=lead_id, to_agent_id=payload.to_agent_id, actor=admin
    )


# ------------------------------------------------------------ interactions

@router.get(
    "/{lead_id}/interactions",
    response_model=list[InteractionOut],
    summary="Conversation timeline",
)
async def list_interactions(lead_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_interactions(session, lead_id=lead_id, agency_id=agent.agency_id)


@router.post(
    "/{lead_id}/interactions",
    response_model=InteractionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a message, call or note",
    description="The first OUTBOUND interaction on a lead is what the response-time "
                "metric measures, so log agent replies here.",
)
async def add_interaction(
    lead_id: uuid.UUID, payload: InteractionCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.add_interaction(session, lead_id=lead_id, agent=agent, data=payload)


# ------------------------------------------------------------------ tasks

@router.get("/{lead_id}/tasks", response_model=list[TaskOut], summary="Follow-up tasks")
async def list_tasks(lead_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_tasks(session, lead_id=lead_id, agency_id=agent.agency_id)


@router.post(
    "/{lead_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a follow-up task",
)
async def create_task(
    lead_id: uuid.UUID, payload: TaskCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.create_task(
        session, lead_id=lead_id, agent=agent, due_at=payload.due_at, note=payload.note
    )


@router.patch(
    "/{lead_id}/tasks/{task_id}",
    response_model=TaskOut,
    summary="Complete, snooze or edit a task",
    responses={404: {"model": Message, "description": "Task not found on this lead"}},
)
async def patch_task(
    lead_id: uuid.UUID, task_id: uuid.UUID, payload: TaskPatch,
    agent: CurrentAgent, session: DbSession,
):
    return await svc.patch_task(
        session, lead_id=lead_id, task_id=task_id, agency_id=agent.agency_id, data=payload
    )

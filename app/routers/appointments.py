"""Visits: booking, the confirmation flow, the client's .ics, post-visit feedback.

The rules (who confirms, what an AI agent may do, how visits move the funnel)
live in services/appointment.py; docs/DECISIONS.md §19.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.deps import CurrentAgent, DbSession, has_scope, require_scope
from app.schemas import (
    AppointmentCreate, AppointmentDetail, AppointmentOut, AppointmentPatch,
    FeedbackCreate, FeedbackOut, Message,
)
from app.services import appointment as svc
from app.services import calendar

# Two routers: visits hang off a lead, but confirming or editing one does not.
lead_router = APIRouter(prefix="/leads", tags=["appointments"])
router = APIRouter(prefix="/appointments", tags=["appointments"])


@lead_router.get(
    "/{lead_id}/appointments",
    response_model=list[AppointmentOut],
    summary="Visits for this lead",
)
async def list_appointments(lead_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_appointments(session, lead_id=lead_id, agency_id=agent.agency_id)


@lead_router.post(
    "/{lead_id}/appointments",
    response_model=AppointmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Book a visit",
    description=(
        "HU-02. **Who confirms:** a visit booked by the lead's owner is born "
        "`CONFIRMED`; one booked by anybody else — an AI agent on the client's behalf, "
        "a colleague — lands `PENDING_CONFIRMATION` for the owner to confirm. An AI "
        "agent granted `visits:manage` books `CONFIRMED` (instant booking).\n\n"
        "**AI agents** book only inside `GET /agents/{id}/slots` and at least "
        "`VISIT_MIN_NOTICE_MINUTES` (120) ahead. **One open visit per lead:** posting "
        "the same slot again returns the existing visit with **200** (safe to retry); "
        "any other time is **409** — move the open one with PATCH.\n\n"
        "Confirming a visit on an `INTERESTED` lead moves it to `VISIT_SCHEDULED`. "
        "Double-booking is prevented here rather than by a database constraint (a "
        "per-agent advisory lock + `SELECT ... FOR UPDATE`); back-to-back is fine."
    ),
    responses={
        200: {"description": "The lead already had this exact visit; it is returned"},
        409: {"model": Message, "description": "Overlap, another open visit on the lead, "
                                               "a closed lead, or (AI agents) outside slots"},
        422: {"model": Message, "description": "In the past, or (AI agents) too soon"},
    },
    dependencies=[Depends(require_scope("visits:request"))],
)
async def request_visit(
    lead_id: uuid.UUID, payload: AppointmentCreate, agent: CurrentAgent,
    session: DbSession, response: Response,
):
    appointment, created = await svc.request_visit(
        session, lead_id=lead_id, scheduled_at=payload.scheduled_at,
        duration_min=payload.duration_min, agent=agent,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return appointment


@router.get(
    "/{appointment_id}",
    response_model=AppointmentDetail,
    summary="One appointment, with where it is",
    description="Adds the property's `location`, the agent's name and a "
                "`google_calendar_url` the client can tap to save the visit.",
    responses={404: {"model": Message, "description": "Not found, or not in your agency"}},
)
async def get_appointment(appointment_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    v = await calendar.visit_detail(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )
    return AppointmentDetail(
        **AppointmentOut.model_validate(v.appointment).model_dump(),
        listing_id=v.listing_id, location=v.location, agent_name=v.agent_name,
        google_calendar_url=calendar.google_calendar_url(v),
    )


@router.get(
    "/{appointment_id}/invite.ics",
    summary="The visit as an .ics file for the client",
    description="What the bot sends the client as a document: opens in Apple Calendar, "
                "Outlook and most desktop calendars. Contains nothing about the client. "
                "Re-sending it after a change updates the event (same UID).",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}, 404: {"model": Message}},
)
async def invite(appointment_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    body = await calendar.invite_ics(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )
    return Response(
        content=body, media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="visita.ics"'},
    )


@router.patch(
    "/{appointment_id}",
    response_model=AppointmentOut,
    summary="Confirm, reschedule, cancel, complete or mark no-show",
    description=(
        "Set `status` to move the visit through its lifecycle, and/or `scheduled_at` / "
        "`duration_min` to move it in time — a move re-runs the overlap check.\n\n"
        "Rescheduling a `CONFIRMED` visit without naming a status marks it "
        "`RESCHEDULED` (the owner confirms their own move by sending `CONFIRMED` "
        "with it). `CANCELLED`, `COMPLETED` and `NO_SHOW` are terminal. `CONFIRMED` "
        "moves an `INTERESTED` lead to `VISIT_SCHEDULED`; `COMPLETED` moves a "
        "`VISIT_SCHEDULED` lead to `VISITED`.\n\n"
        "**AI agents** (`visits:request`) may move or cancel a visit for the client; "
        "confirming needs `visits:manage`, and only people record `COMPLETED` / "
        "`NO_SHOW`."
    ),
    responses={
        403: {"model": Message, "description": "An AI agent confirming without "
                                               "`visits:manage`, or recording an outcome"},
        409: {"model": Message, "description": "Already terminal, or the new slot is taken"},
        422: {"model": Message, "description": "New scheduled_at in the past or too soon"},
    },
    dependencies=[Depends(require_scope("visits:request"))],
)
async def patch_appointment(
    appointment_id: uuid.UUID, payload: AppointmentPatch,
    agent: CurrentAgent, session: DbSession,
):
    # Same shape as leads:close on transitions: the route scope lets a bot in,
    # the decision it may take is narrower.
    if payload.status == "CONFIRMED" and not has_scope(agent, "visits:manage"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Service account lacks the 'visits:manage' scope required to confirm a "
            "visit — the owning agent confirms",
        )
    if agent.is_bot and payload.status in svc.HUMAN_ONLY_STATUSES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a person who was there records a visit's outcome (COMPLETED, NO_SHOW)",
        )
    return await svc.patch_appointment(
        session, appointment_id=appointment_id, agent=agent, data=payload
    )


@router.get(
    "/{appointment_id}/feedback",
    response_model=list[FeedbackOut],
    summary="Feedback left on this visit",
    description="At most one row per side (`AGENT`, `CLIENT`). The bot checks here "
                "whether the client has already been asked.",
    responses={404: {"model": Message}},
)
async def list_feedback(appointment_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.list_feedback(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )


@router.post(
    "/{appointment_id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record post-visit feedback",
    description="Only valid once the visit is `COMPLETED`. `objection` accepts the "
                "codes in `core.objection`: PRICE, SIZE, LOCATION, CONDITION, "
                "HOA_FEE, OTHER. One per side: a second post from the same side "
                "returns the first with **200**. An AI agent records only "
                "`submitted_by: CLIENT`.",
    responses={
        200: {"description": "That side already left feedback; it is returned"},
        403: {"model": Message, "description": "An AI agent submitting as AGENT"},
        409: {"model": Message, "description": "The visit is not COMPLETED"},
        422: {"model": Message, "description": "Unknown objection code"},
    },
    dependencies=[Depends(require_scope("visits:feedback"))],
)
async def add_feedback(
    appointment_id: uuid.UUID, payload: FeedbackCreate,
    agent: CurrentAgent, session: DbSession, response: Response,
):
    feedback, created = await svc.add_feedback(
        session, appointment_id=appointment_id, agent=agent, data=payload
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return feedback

"""Visit requests, confirmation flow and post-visit feedback."""
import uuid

from fastapi import APIRouter, status

from app.deps import CurrentAgent, DbSession
from app.schemas import (
    AppointmentCreate, AppointmentOut, AppointmentPatch, FeedbackCreate,
    FeedbackOut, Message,
)
from app.services import appointment as svc

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
    summary="Request a visit",
    description=(
        "HU-02. Agent availability tables were cut from scope, so this is *request a "
        "visit* — it lands as `PENDING_CONFIRMATION` for the agent to confirm — not "
        "book-a-free-slot.\n\n"
        "Double-booking is prevented here rather than by a database constraint: the "
        "`EXCLUDE USING gist` constraint could not be created because an inline "
        "`tstzrange` is not IMMUTABLE. The check locks the agent's overlapping rows "
        "with `SELECT ... FOR UPDATE` inside the transaction, so it is safe under "
        "concurrency. Back-to-back visits are allowed; genuine overlaps get **409**."
    ),
    responses={
        409: {"model": Message, "description": "Overlaps a visit the agent already has"},
        422: {"model": Message, "description": "scheduled_at is in the past"},
    },
)
async def request_visit(
    lead_id: uuid.UUID, payload: AppointmentCreate, agent: CurrentAgent, session: DbSession
):
    return await svc.request_visit(
        session, lead_id=lead_id, scheduled_at=payload.scheduled_at,
        duration_min=payload.duration_min, agent=agent,
    )


@router.get(
    "/{appointment_id}",
    response_model=AppointmentOut,
    summary="One appointment",
    responses={404: {"model": Message, "description": "Not found, or not in your agency"}},
)
async def get_appointment(appointment_id: uuid.UUID, agent: CurrentAgent, session: DbSession):
    return await svc.get_appointment(
        session, appointment_id=appointment_id, agency_id=agent.agency_id
    )


@router.patch(
    "/{appointment_id}",
    response_model=AppointmentOut,
    summary="Confirm, reschedule, cancel, complete or mark no-show",
    description=(
        "Set `status` to move the visit through its lifecycle, and/or `scheduled_at` / "
        "`duration_min` to move it in time — a move re-runs the overlap check.\n\n"
        "Rescheduling a `CONFIRMED` visit without naming a status marks it "
        "`RESCHEDULED`. `CANCELLED`, `COMPLETED` and `NO_SHOW` are terminal."
    ),
    responses={
        409: {"model": Message, "description": "Already terminal, or the new slot overlaps"},
        422: {"model": Message, "description": "New scheduled_at is in the past"},
    },
)
async def patch_appointment(
    appointment_id: uuid.UUID, payload: AppointmentPatch,
    agent: CurrentAgent, session: DbSession,
):
    return await svc.patch_appointment(
        session, appointment_id=appointment_id, agent=agent, data=payload
    )


@router.post(
    "/{appointment_id}/feedback",
    response_model=FeedbackOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record post-visit feedback",
    description="Only valid once the visit is `COMPLETED`. `objection` accepts the "
                "codes in `core.objection`: PRICE, SIZE, LOCATION, CONDITION, "
                "HOA_FEE, OTHER.",
    responses={
        409: {"model": Message, "description": "The visit is not COMPLETED"},
        422: {"model": Message, "description": "Unknown objection code"},
    },
)
async def add_feedback(
    appointment_id: uuid.UUID, payload: FeedbackCreate,
    agent: CurrentAgent, session: DbSession,
):
    return await svc.add_feedback(
        session, appointment_id=appointment_id, agent=agent, data=payload
    )

"""Pydantic v2 request/response models.

Value sets are text + CHECK in the database on purpose (editable with a plain
ALTER). They are mirrored here as Literals so the API rejects bad values with a
422 rather than a 500 from a constraint violation. Keep the two in step.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Stage = Literal["INTERESTED", "VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON", "LOST"]
Channel = Literal["WHATSAPP", "IN_APP", "CALL"]
Direction = Literal["INBOUND", "OUTBOUND"]
InteractionType = Literal["MESSAGE", "CALL", "NOTE", "STATUS_CHANGE"]
AppointmentStatus = Literal[
    "PENDING_CONFIRMATION", "CONFIRMED", "RESCHEDULED", "CANCELLED", "COMPLETED", "NO_SHOW"
]
TaskStatus = Literal["PENDING", "DONE", "SNOOZED"]
OperationType = Literal["SALE", "RENT"]
ListingStatus = Literal["ACTIVE", "PAUSED", "CLOSED"]
SubmittedBy = Literal["AGENT", "CLIENT"]

TERMINAL_STAGES: frozenset[str] = frozenset({"WON", "LOST"})

# The legal funnel. Enforced in the service layer: the schema stores transitions
# but does not constrain which edges are allowed.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "INTERESTED":      frozenset({"VISIT_SCHEDULED", "LOST"}),
    "VISIT_SCHEDULED": frozenset({"VISITED", "LOST"}),
    "VISITED":         frozenset({"NEGOTIATING", "LOST"}),
    "NEGOTIATING":     frozenset({"WON", "LOST"}),
    "WON":             frozenset(),
    "LOST":            frozenset(),
}


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------- identity

class AgentOut(ORMModel):
    id: uuid.UUID
    agency_id: uuid.UUID
    role: Literal["AGENT", "TEAM_ADMIN"]
    active: bool
    full_name: str | None = None
    email: str | None = None


# --------------------------------------------------------------------- lead

class LeadCreate(BaseModel):
    client_id: uuid.UUID
    listing_id: uuid.UUID
    source_channel: Channel
    # Optional opening message, recorded as the first INBOUND interaction so the
    # response-time clock has something to start from.
    message: str | None = Field(default=None, max_length=4000)


class LeadOut(ORMModel):
    id: uuid.UUID
    client_id: uuid.UUID
    listing_id: uuid.UUID
    agent_id: uuid.UUID
    source_channel: Channel
    current_stage: Stage
    created_at: datetime
    updated_at: datetime


class LeadDetail(LeadOut):
    client_name: str | None = None
    listing_address: str | None = None
    asking_price: Decimal | None = None
    operation_type: OperationType | None = None


class TransitionCreate(BaseModel):
    to_stage: Stage
    # Required when to_stage is LOST. The schema cannot express this, so the
    # service writes lead_lost_detail in the same transaction.
    lost_reason: Literal[
        "PRICE", "LOCATION", "BOUGHT_ELSEWHERE", "NO_RESPONSE", "FINANCING", "OTHER"
    ] | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _lost_needs_reason(self):
        if self.to_stage == "LOST" and self.lost_reason is None:
            raise ValueError("lost_reason is required when moving a lead to LOST")
        if self.to_stage != "LOST" and self.lost_reason is not None:
            raise ValueError("lost_reason is only valid when moving a lead to LOST")
        return self


class TransitionOut(ORMModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    from_stage: Stage | None
    to_stage: Stage
    changed_by: uuid.UUID | None
    changed_at: datetime


class ReassignRequest(BaseModel):
    to_agent_id: uuid.UUID


# -------------------------------------------------------------- interaction

class InteractionCreate(BaseModel):
    direction: Direction
    channel: Channel
    type: InteractionType = "MESSAGE"
    body: str | None = Field(default=None, max_length=4000)
    occurred_at: datetime | None = None


class InteractionOut(ORMModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    direction: Direction
    channel: Channel
    type: InteractionType
    body: str | None
    occurred_at: datetime
    created_by: uuid.UUID | None


# -------------------------------------------------------------- appointment

class AppointmentCreate(BaseModel):
    scheduled_at: datetime
    duration_min: int = Field(default=60, ge=15, le=480)


class AppointmentPatch(BaseModel):
    status: AppointmentStatus | None = None
    scheduled_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=15, le=480)

    @model_validator(mode="after")
    def _something_to_do(self):
        if self.status is None and self.scheduled_at is None and self.duration_min is None:
            raise ValueError("provide at least one of: status, scheduled_at, duration_min")
        return self


class AppointmentOut(ORMModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    agent_id: uuid.UUID
    scheduled_at: datetime
    duration_min: int
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime


class FeedbackCreate(BaseModel):
    submitted_by: SubmittedBy
    interest_score: int | None = Field(default=None, ge=1, le=5)
    # core.objection codes, verified against the live lookup table.
    objection: Literal["PRICE", "SIZE", "LOCATION", "CONDITION", "HOA_FEE", "OTHER"] | None = None
    close_probability: Decimal | None = Field(default=None, ge=0, le=1)
    free_text: str | None = Field(default=None, max_length=2000)


class FeedbackOut(ORMModel):
    id: uuid.UUID
    appointment_id: uuid.UUID
    submitted_by: SubmittedBy
    interest_score: int | None
    objection_id: uuid.UUID | None
    close_probability: Decimal | None
    free_text: str | None
    created_at: datetime


# --------------------------------------------------------------------- task

class TaskCreate(BaseModel):
    due_at: datetime
    note: str | None = Field(default=None, max_length=1000)


class TaskPatch(BaseModel):
    status: TaskStatus | None = None
    due_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _something_to_do(self):
        if self.status is None and self.due_at is None and self.note is None:
            raise ValueError("provide at least one of: status, due_at, note")
        return self


class TaskOut(ORMModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    agent_id: uuid.UUID
    due_at: datetime
    note: str | None
    status: TaskStatus
    created_at: datetime


# ------------------------------------------------------------------ listing

class ListingOut(ORMModel):
    id: uuid.UUID
    property_id: uuid.UUID
    agent_id: uuid.UUID
    operation_type: OperationType
    asking_price: Decimal
    status: ListingStatus
    published_at: datetime
    city: str | None = None
    neighborhood: str | None = None
    address: str | None = None
    property_type: str | None = None
    area_m2: Decimal | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None


class ViewCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    client_id: uuid.UUID | None = None


# ---------------------------------------------------------------- analytics

class FunnelDailyOut(BaseModel):
    day: date
    to_stage: Stage
    transitions: int


class AgentResponseTimeOut(BaseModel):
    agent_id: uuid.UUID
    agent_name: str | None = None
    leads: int
    avg_first_response_hours: float | None
    median_first_response_hours: float | None
    never_answered: int


class ListingPerformanceOut(BaseModel):
    listing_id: uuid.UUID
    operation_type: OperationType
    city: str
    neighborhood: str
    property_type: str
    asking_price: Decimal
    views: int
    leads: int
    visits: int
    won: int


class NorthStarOut(BaseModel):
    """The five metrics the whole schema exists to make measurable."""
    leads: int
    median_first_response_hours: float | None
    pct_with_follow_up: float
    lead_to_visit_conversion_pct: float
    pct_lost_within_48h: float
    stage_conversion: list["StageConversionOut"]


class StageConversionOut(BaseModel):
    stage: Stage
    sort_order: int
    leads_reached: int
    leads_prev_stage: int | None
    pct_from_prev: float | None


NorthStarOut.model_rebuild()


# ------------------------------------------------------------------- common

class Message(BaseModel):
    detail: str

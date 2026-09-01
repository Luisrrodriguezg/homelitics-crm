"""SQLAlchemy 2.0 models.

Mirrors schema-2.sql. Only the tables the API actually touches are mapped;
the analytics schema is deliberately absent — those are plain views with no
primary key and are queried as raw SQL in services/analytics.py.

Value sets are text + CHECK in the database (deliberately, so they are editable
with a plain ALTER). They are mirrored as plain str here and validated in
schemas.py, not as SQLAlchemy Enums, so adding a value needs no model change.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric,
    SmallInteger, Text, Time, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def _ts(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


# --------------------------------------------------------------------- pii

class Person(Base):
    __tablename__ = "person"
    __table_args__ = {"schema": "pii"}

    id: Mapped[uuid.UUID] = _pk()
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    national_id: Mapped[str | None] = mapped_column(Text)
    # Right-to-erasure: scrubbing this one row anonymises the human everywhere,
    # because funnel facts reference role ids (agent/client/owner), never names.
    anonymized_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())


# -------------------------------------------------------------- core: org

class Agency(Base):
    __tablename__ = "agency"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class Agent(Base):
    __tablename__ = "agent"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pii.person.id"), nullable=False)
    agency_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agency.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="AGENT")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # The JWT `sub` claim. Nullable: seeded agents are unbound until
    # scripts/bind_agents.py runs. Unique index allows many NULLs.
    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    created_at: Mapped[datetime] = _ts(server_default=func.now())

    person: Mapped[Person] = relationship(lazy="joined")


class Owner(Base):
    __tablename__ = "owner"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pii.person.id"), nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class Client(Base):
    __tablename__ = "client"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pii.person.id"), nullable=False)
    created_at: Mapped[datetime] = _ts(server_default=func.now())

    person: Mapped[Person] = relationship(lazy="joined")


# -------------------------------------------------- core: inventory

class Property(Base):
    __tablename__ = "property"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.owner.id"), nullable=False)
    property_type: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    neighborhood: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    area_m2: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    bedrooms: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bathrooms: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    parking_spots: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    year_built: Mapped[int | None] = mapped_column(SmallInteger)
    hoa_fee: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())


class Listing(Base):
    """The commercial act. One table for SALE and RENT — never split."""
    __tablename__ = "listing"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.property.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    asking_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    min_acceptable_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")
    published_at: Mapped[datetime] = _ts(server_default=func.now())
    closed_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())

    property: Mapped[Property] = relationship(lazy="joined")


# ----------------------------------------------------- core: funnel

class LeadStage(Base):
    __tablename__ = "lead_stage"
    __table_args__ = {"schema": "core"}

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class LostReason(Base):
    __tablename__ = "lost_reason"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class Lead(Base):
    """The product. UNIQUE (client_id, listing_id) *is* the dedup requirement."""
    __tablename__ = "lead"
    __table_args__ = (
        UniqueConstraint("client_id", "listing_id", name="lead_client_id_listing_id_key"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = _pk()
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.client.id"), nullable=False)
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.listing.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    source_channel: Mapped[str] = mapped_column(Text, nullable=False)
    # A CACHE. core.lead_stage_transition is the truth; a trigger maintains this.
    current_stage: Mapped[str] = mapped_column(
        ForeignKey("core.lead_stage.code"), nullable=False, server_default="INTERESTED"
    )
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())


class LeadStageTransition(Base):
    """Append-only. This is the truth; lead.current_stage is derived from it."""
    __tablename__ = "lead_stage_transition"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    from_stage: Mapped[str | None] = mapped_column(ForeignKey("core.lead_stage.code"))
    to_stage: Mapped[str] = mapped_column(ForeignKey("core.lead_stage.code"), nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.agent.id"))
    changed_at: Mapped[datetime] = _ts(server_default=func.now())


class LeadLostDetail(Base):
    __tablename__ = "lead_lost_detail"
    __table_args__ = {"schema": "core"}

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), primary_key=True)
    lost_reason_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lost_reason.id"), nullable=False)
    free_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class Interaction(Base):
    """Append-only timeline. first OUTBOUND drives the response-time metric."""
    __tablename__ = "interaction"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False, server_default="MESSAGE")
    body: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = _ts(server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.agent.id"))


class Appointment(Base):
    __tablename__ = "appointment"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    scheduled_at: Mapped[datetime] = _ts(nullable=False)
    duration_min: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="60")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING_CONFIRMATION")
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    updated_at: Mapped[datetime] = _ts(server_default=func.now())


class Objection(Base):
    __tablename__ = "objection"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class VisitFeedback(Base):
    __tablename__ = "visit_feedback"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.appointment.id"), nullable=False)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    interest_score: Mapped[int | None] = mapped_column(SmallInteger)
    objection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.objection.id"))
    close_probability: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    free_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class FollowUpTask(Base):
    __tablename__ = "follow_up_task"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    due_at: Mapped[datetime] = _ts(nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class AssignmentAudit(Base):
    __tablename__ = "assignment_audit"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    from_agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    to_agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    reassigned_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    reassigned_at: Mapped[datetime] = _ts(server_default=func.now())


class Offer(Base):
    __tablename__ = "offer"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    offered_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    offered_at: Mapped[datetime] = _ts(server_default=func.now())


class Deal(Base):
    __tablename__ = "deal"
    __table_args__ = {"schema": "core"}

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.lead.id"), primary_key=True)
    closed_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="0")
    closed_at: Mapped[datetime] = _ts(nullable=False)
    contract_start: Mapped[date | None] = mapped_column(Date)
    contract_months: Mapped[int | None] = mapped_column(SmallInteger)


# -------------------------------------------------- core: availability

class AgentAvailability(Base):
    """A weekly recurring block an agent is reachable. weekday: 0=Mon..6=Sun."""
    __tablename__ = "agent_availability"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class AgentTimeOff(Base):
    """Ad-hoc unavailability. Half-open [starts_at, ends_at)."""
    __tablename__ = "agent_time_off"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = _pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.agent.id"), nullable=False)
    starts_at: Mapped[datetime] = _ts(nullable=False)
    ends_at: Mapped[datetime] = _ts(nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


# ------------------------------------------------------------ events

class PropertyView(Base):
    __tablename__ = "property_view"
    __table_args__ = {"schema": "events"}

    id: Mapped[uuid.UUID] = _pk()
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.listing.id"), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.client.id"))
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    viewed_at: Mapped[datetime] = _ts(server_default=func.now())


class DomainEvent(Base):
    """Transactional outbox. The API inserts a row in the caller's transaction;
    the jobs.relay_events job publishes it and stamps published_at."""
    __tablename__ = "domain_event"
    __table_args__ = {"schema": "events"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[datetime] = _ts(server_default=func.now())
    published_at: Mapped[datetime | None] = _ts()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

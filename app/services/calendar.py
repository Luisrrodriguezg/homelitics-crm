"""Views of the visit calendar (009). Everything here reads; nothing decides.

core.appointment is the calendar and services/appointment.py holds its rules.
This module only renders it, four ways:

* calendar_events     -> JSON for the frontend, shaped for FullCalendar;
* feed_ics            -> an agent's private .ics feed (Google/Apple/Outlook subscribe);
* invite_ics          -> one visit as an .ics file the bot hands the client;
* google_calendar_url -> the same visit as an "Add to Google Calendar" link.

Times leave in UTC ('Z'), so no VTIMEZONE block is needed and every client
renders them in its own zone. UIDs are stable ('<appointment id>@homelitics'),
so a refreshed feed or a re-sent invite updates the event instead of
duplicating it. docs/DECISIONS.md §19.
"""
from __future__ import annotations

import hmac
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from icalendar import Calendar, Event
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.models import (
    Agent, AgentAvailability, AgentTimeOff, Appointment, Client, Lead, Listing, Person, Property,
)
from app.services import calendar_import
from app.services.appointment import _BLOCKING, get_appointment
from app.services.availability import _agent_in_agency, expand_rules

# A frontend asks for a month at a time; this is a month plus slack either side.
MAX_WINDOW = timedelta(days=62)
# What an agent's subscribed feed carries: recent history and the next half-year.
FEED_PAST = timedelta(days=30)
FEED_FUTURE = timedelta(days=180)

_LABEL = {
    "PENDING_CONFIRMATION": "por confirmar",
    "RESCHEDULED": "reprogramada, por confirmar",
    "CONFIRMED": "confirmada",
    "COMPLETED": "realizada",
    "NO_SHOW": "el cliente no asistió",
    "CANCELLED": "cancelada",
}
_ICS_STATUS = {
    "PENDING_CONFIRMATION": "TENTATIVE",
    "RESCHEDULED": "TENTATIVE",
    "CONFIRMED": "CONFIRMED",
    "COMPLETED": "CONFIRMED",
    "NO_SHOW": "CONFIRMED",
    "CANCELLED": "CANCELLED",
}


@dataclass
class Visit:
    """One appointment plus what it takes to describe it outside the CRM."""
    appointment: Appointment
    listing_id: uuid.UUID
    address: str
    neighborhood: str
    city: str
    client_name: str | None
    agent_name: str | None
    booked_by_bot: bool

    @property
    def start(self) -> datetime:
        return self.appointment.scheduled_at.astimezone(timezone.utc)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.appointment.duration_min)

    @property
    def location(self) -> str:
        return f"{self.address}, {self.neighborhood}, {self.city}"

    @property
    def client_first_name(self) -> str:
        # First name only: the feed leaves the CRM, into Google's servers.
        return (self.client_name or "").split(" ", 1)[0] or "cliente"


async def _load_visits(session: AsyncSession, *conditions) -> list[Visit]:
    client_person, agent_row, agent_person, booker = (
        aliased(Person), aliased(Agent), aliased(Person), aliased(Agent)
    )
    rows = (
        await session.execute(
            select(
                Appointment, Lead.listing_id, Property.address, Property.neighborhood,
                Property.city, client_person.full_name, agent_person.full_name, booker.role,
            )
            .join(Lead, Lead.id == Appointment.lead_id)
            .join(Listing, Listing.id == Lead.listing_id)
            .join(Property, Property.id == Listing.property_id)
            .join(Client, Client.id == Lead.client_id)
            .join(client_person, client_person.id == Client.person_id)
            .join(agent_row, agent_row.id == Appointment.agent_id)
            .join(agent_person, agent_person.id == agent_row.person_id)
            .outerjoin(booker, booker.id == Appointment.created_by)
            .where(*conditions)
            .order_by(Appointment.scheduled_at)
        )
    ).all()
    return [
        Visit(appointment=a, listing_id=listing_id, address=address, neighborhood=hood,
              city=city, client_name=client, agent_name=agent_name,
              booked_by_bot=booker_role == "AI_AGENT")
        for a, listing_id, address, hood, city, client, agent_name, booker_role in rows
    ]


def _title(v: Visit) -> str:
    title = f"Visita · {v.client_first_name} · {v.neighborhood}"
    if v.appointment.status != "CONFIRMED":
        title += f" ({_LABEL[v.appointment.status]})"
    return title


# ------------------------------------------------------------------ JSON view

async def calendar_events(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    start: datetime,
    end: datetime,
    agent_id: uuid.UUID | None = None,
) -> list[dict]:
    """Visits and time off in [start, end) — one agent's (plus their published
    hours as a background), or the whole agency's when agent_id is None.

    Agency-scoped like every read: an agent of another agency is a 404.
    """
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`from` must be before `to`")
    if end - start > MAX_WINDOW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Ask for at most {MAX_WINDOW.days} days at a time")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    agents_q = select(Agent.id).where(Agent.agency_id == agency_id)
    if agent_id is not None:
        await _agent_in_agency(session, agent_id, agency_id)
        await calendar_import.maybe_refresh(session, agent_id=agent_id)
        agents_q = agents_q.where(Agent.id == agent_id)

    appt_end = Appointment.scheduled_at + func.make_interval(
        0, 0, 0, 0, 0, Appointment.duration_min
    )
    visits = await _load_visits(
        session,
        Appointment.agent_id.in_(agents_q),
        Appointment.scheduled_at < end,
        appt_end > start,
    )
    time_off = (
        await session.execute(
            select(AgentTimeOff, Person.full_name)
            .join(Agent, Agent.id == AgentTimeOff.agent_id)
            .join(Person, Person.id == Agent.person_id)
            .where(
                AgentTimeOff.agent_id.in_(agents_q),
                AgentTimeOff.starts_at < end,
                AgentTimeOff.ends_at > start,
            )
            .order_by(AgentTimeOff.starts_at)
        )
    ).all()

    imported = [(t.agent_id, t.starts_at, t.ends_at) for t, _ in time_off if t.source == "ICS"]
    events: list[dict] = []
    for v in visits:
        a = v.appointment
        events.append({
            "id": str(a.id), "kind": "VISIT", "agent_id": a.agent_id,
            "agent_name": v.agent_name, "start": v.start, "end": v.end, "title": _title(v),
            "status": a.status, "lead_id": a.lead_id, "listing_id": v.listing_id,
            "location": v.location, "booked_by_bot": v.booked_by_bot,
            # Manual time off can never cover a booked visit (it 409s); busy
            # time imported from the agent's own calendar can. They decide.
            "conflict": a.status in _BLOCKING and any(
                aid == a.agent_id and s < v.end and v.start < e for aid, s, e in imported
            ),
        })
    for t, name in time_off:
        events.append({
            "id": str(t.id), "kind": "TIME_OFF", "agent_id": t.agent_id, "agent_name": name,
            "start": t.starts_at, "end": t.ends_at, "source": t.source,
            "title": "Ocupado (calendario externo)" if t.source == "ICS"
                     else (t.reason or "No disponible"),
        })

    if agent_id is not None:
        rules = (
            await session.execute(
                select(AgentAvailability).where(AgentAvailability.agent_id == agent_id)
            )
        ).scalars().all()
        tz = ZoneInfo(get_settings().app_timezone)
        for block_start, block_end in expand_rules(rules, start, end, tz):
            s, e = max(block_start, start), min(block_end, end)
            if s < e:
                events.append({
                    "id": f"availability-{agent_id}-{s:%Y%m%dT%H%M}", "kind": "AVAILABILITY",
                    "agent_id": agent_id, "start": s, "end": e, "title": "Disponible",
                })

    events.sort(key=lambda ev: (ev["start"], ev["kind"]))
    return events


# ------------------------------------------------------------------ .ics

def _new_calendar(*, name: str | None = None) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//Homelitics//Visitas//ES")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    if name is not None:
        cal.add("x-wr-calname", name)
        # Hints for a subscribed feed. Apple and Outlook may honour them;
        # Google refreshes on its own schedule (hours) whatever this says.
        cal.add("refresh-interval", timedelta(minutes=15), parameters={"VALUE": "DURATION"})
        cal.add("x-published-ttl", "PT15M")
    return cal


def _vevent(v: Visit, *, summary: str, description: str, now: datetime) -> Event:
    a = v.appointment
    ev = Event()
    ev.add("uid", f"{a.id}{calendar_import.OWN_UID_SUFFIX}")
    ev.add("dtstamp", now)
    ev.add("dtstart", v.start)
    ev.add("dtend", v.end)
    ev.add("summary", summary)
    ev.add("location", v.location)
    ev.add("description", description)
    ev.add("status", _ICS_STATUS[a.status])
    # Grows with every change, so a calendar app takes the newer copy of the
    # same UID instead of keeping the one it already has. Rounded up: a change
    # in the visit's first second must still move it off 0.
    ev.add("sequence", max(0, math.ceil((a.updated_at - a.created_at).total_seconds())))
    ev.add("last-modified", a.updated_at.astimezone(timezone.utc))
    ev.add("transp", "OPAQUE")
    return ev


async def feed_ics(session: AsyncSession, *, agent_id: uuid.UUID, token: str) -> bytes:
    """An agent's visits as a subscribable calendar. The token in the URL is
    the only credential — calendar apps send no Authorization header — so any
    mismatch is a plain 404 that confirms nothing about the agent id."""
    agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    try:
        given = str(uuid.UUID(token))
    except ValueError:
        given = ""
    if (
        agent is None or agent.is_bot or not agent.active
        or not hmac.compare_digest(given, str(agent.calendar_token))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Calendar not found")

    now = datetime.now(timezone.utc)
    visits = await _load_visits(
        session,
        Appointment.agent_id == agent_id,
        Appointment.status != "CANCELLED",      # a cancelled visit just disappears
        Appointment.scheduled_at >= now - FEED_PAST,
        Appointment.scheduled_at < now + FEED_FUTURE,
    )
    cal = _new_calendar(name=f"Visitas Homelitics · {agent.person.full_name}")
    for v in visits:
        lines = [f"Cliente: {v.client_first_name}", f"Estado: {_LABEL[v.appointment.status]}"]
        if v.booked_by_bot:
            lines.append("Agendada por el asistente virtual")
        cal.add_component(_vevent(v, summary=_title(v), description="\n".join(lines), now=now))
    return cal.to_ical()


async def visit_detail(
    session: AsyncSession, *, appointment_id: uuid.UUID, agency_id: uuid.UUID
) -> Visit:
    await get_appointment(session, appointment_id=appointment_id, agency_id=agency_id)  # 404
    return (await _load_visits(session, Appointment.id == appointment_id))[0]


def _client_summary(v: Visit) -> str:
    return f"Visita inmobiliaria · {v.neighborhood}"


def _client_details(v: Visit) -> str:
    lines = [f"Visita al inmueble en {v.address}, {v.neighborhood}."]
    if v.agent_name:
        lines.append(f"Agente: {v.agent_name}")
    lines.append(f"Estado: {_LABEL[v.appointment.status]}")
    return "\n".join(lines)


async def invite_ics(
    session: AsyncSession, *, appointment_id: uuid.UUID, agency_id: uuid.UUID
) -> bytes:
    """One visit, for the client to add to their own calendar. Carries nothing
    about the client — it is their own event."""
    v = await visit_detail(session, appointment_id=appointment_id, agency_id=agency_id)
    cal = _new_calendar()
    cal.add_component(
        _vevent(v, summary=_client_summary(v), description=_client_details(v),
                now=datetime.now(timezone.utc))
    )
    return cal.to_ical()


def google_calendar_url(v: Visit) -> str:
    """The same visit as a Google Calendar link — what Android users need,
    since the Calendar app there cannot open an .ics file."""
    fmt = "%Y%m%dT%H%M%SZ"
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": _client_summary(v),
        "dates": f"{v.start:{fmt}}/{v.end:{fmt}}",
        "location": v.location,
        "details": _client_details(v),
        "ctz": get_settings().app_timezone,
    })


# ------------------------------------------------------------------ feed URL

def feed_urls(*, agent_id: uuid.UUID, token: uuid.UUID, base_url: str) -> dict:
    base = (get_settings().public_base_url or base_url).rstrip("/")
    https = f"{base}/agents/{agent_id}/calendar.ics?token={token}"
    return {"ics_url": https, "webcal_url": "webcal://" + https.split("://", 1)[1]}


async def rotate_feed_token(session: AsyncSession, *, agent_id: uuid.UUID) -> uuid.UUID:
    """A new secret; the old feed URL is a 404 from the next fetch on."""
    token = (
        await session.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(calendar_token=func.gen_random_uuid())
            .returning(Agent.calendar_token)
        )
    ).scalar_one()
    await session.commit()
    return token

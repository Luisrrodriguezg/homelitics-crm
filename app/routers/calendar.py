"""The visit calendar (009): JSON views for the frontend, each agent's private
.ics feed, and the connection to the agent's own (Google) calendar.

core.appointment is the calendar; these routes only show it, or feed busy time
into it. docs/DECISIONS.md §19.

One route here has no `CurrentAgent`: GET /agents/{id}/calendar.ics. Calendar
apps fetch a subscribed URL with no Authorization header, so the token in the
query string is the credential. Everything else is agency-scoped like the rest
of the API.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.config import get_settings
from app.deps import CurrentAgent, DbSession, require_scope
from app.schemas import (
    CalendarFeedOut, CalendarOut, CalendarSyncOut, ExternalCalendarIn,
    ExternalCalendarOut, Message,
)
from app.services import calendar as svc
from app.services import calendar_import

router = APIRouter(tags=["calendar"])

_ICS = "text/calendar; charset=utf-8"


def _human(agent) -> None:
    # An AI agent owns no visits and has no calendar of its own.
    if agent.is_bot:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI agents have no calendar")


def _external_out(cal) -> ExternalCalendarOut:
    return ExternalCalendarOut(
        ics_url_masked=calendar_import.mask_url(cal.ics_url),
        last_synced_at=cal.last_synced_at, last_status=cal.last_status,
        last_error=cal.last_error, created_at=cal.created_at,
    )


# ------------------------------------------------------------------ JSON

@router.get(
    "/agents/{agent_id}/calendar",
    response_model=CalendarOut,
    summary="One agent's calendar",
    description=(
        "Visits (`VISIT`), time off (`TIME_OFF`, manual or imported from the agent's "
        "own calendar) and published hours (`AVAILABILITY`, meant as a background) in "
        "`[from, to)` — at most 62 days. Each event carries `id`, `start`, `end` and "
        "`title`, so FullCalendar can take the list as-is. A visit with `conflict: "
        "true` overlaps imported busy time. **404** for an agent outside your agency."
    ),
    responses={404: {"model": Message}, 422: {"model": Message}},
)
async def agent_calendar(
    agent_id: uuid.UUID,
    agent: CurrentAgent,
    session: DbSession,
    from_: datetime = Query(alias="from", description="window start (ISO 8601)"),
    to: datetime = Query(description="window end (ISO 8601)"),
):
    events = await svc.calendar_events(
        session, agency_id=agent.agency_id, start=from_, end=to, agent_id=agent_id
    )
    return CalendarOut(timezone=get_settings().app_timezone, events=events)


@router.get(
    "/calendar",
    response_model=CalendarOut,
    summary="The whole agency's calendar",
    description="Every agent's visits and time off in `[from, to)`, with `agent_name` "
                "on each event — the team view. No availability background.",
    responses={422: {"model": Message}},
)
async def agency_calendar(
    agent: CurrentAgent,
    session: DbSession,
    from_: datetime = Query(alias="from", description="window start (ISO 8601)"),
    to: datetime = Query(description="window end (ISO 8601)"),
):
    events = await svc.calendar_events(
        session, agency_id=agent.agency_id, start=from_, end=to
    )
    return CalendarOut(timezone=get_settings().app_timezone, events=events)


# ------------------------------------------------------------------ .ics feed

@router.get(
    "/me/calendar-feed",
    response_model=CalendarFeedOut,
    summary="Your private .ics feed URL",
    description=(
        "Paste `ics_url` into Google Calendar (*Other calendars → From URL*) or open "
        "`webcal_url` on a Mac/iPhone to subscribe. The URL is the credential: anyone "
        "holding it can read your visits, so rotate it if it leaks. Google refreshes "
        "subscribed calendars on its own schedule (hours); Apple lets you pick."
    ),
)
async def my_calendar_feed(request: Request, agent: CurrentAgent):
    _human(agent)
    return svc.feed_urls(agent_id=agent.id, token=agent.calendar_token,
                         base_url=str(request.base_url))


@router.post(
    "/me/calendar-feed/rotate",
    response_model=CalendarFeedOut,
    summary="Replace your feed URL",
    description="Issues a new secret. The old URL returns 404 from its next fetch on; "
                "re-subscribe with the new one.",
    dependencies=[Depends(require_scope("calendar:feed"))],
)
async def rotate_calendar_feed(request: Request, agent: CurrentAgent, session: DbSession):
    _human(agent)
    token = await svc.rotate_feed_token(session, agent_id=agent.id)
    return svc.feed_urls(agent_id=agent.id, token=token, base_url=str(request.base_url))


@router.get(
    "/agents/{agent_id}/calendar.ics",
    summary="An agent's visits as a subscribable calendar",
    description=(
        "What calendar apps fetch. **No Authorization header** — the `token` query "
        "parameter from `GET /me/calendar-feed` is the credential; a wrong one is "
        "**404**. Visits from 30 days back to 180 ahead, cancelled ones left out."
    ),
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}, 404: {"model": Message}},
)
async def calendar_feed(
    agent_id: uuid.UUID, session: DbSession, token: str = Query(max_length=64)
):
    body = await svc.feed_ics(session, agent_id=agent_id, token=token)
    return Response(content=body, media_type=_ICS, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ external calendar

_WRITE = [Depends(require_scope("availability:write"))]


@router.get(
    "/me/external-calendar",
    response_model=ExternalCalendarOut,
    summary="Your connected calendar",
    description="The address is masked — it is a credential for your whole calendar.",
    responses={404: {"model": Message, "description": "No calendar connected"}},
)
async def get_external_calendar(agent: CurrentAgent, session: DbSession):
    _human(agent)
    return _external_out(await calendar_import.get_external_calendar(session, agent_id=agent.id))


@router.put(
    "/me/external-calendar",
    response_model=ExternalCalendarOut,
    summary="Connect your Google (or Outlook/iCloud) calendar",
    description=(
        "Give the calendar's **secret iCal address** — Google Calendar → Settings → "
        "your calendar → *Integrate calendar* → *Secret address in iCal format*. Its "
        "busy events for the next 60 days become time off, so the assistant cannot "
        "book over them; only times are kept, never titles. Synced immediately, then "
        "again whenever a calendar read finds it older than 15 minutes. Only "
        "`https://`/`webcal://` addresses on Google, Outlook or iCloud are accepted."
    ),
    responses={422: {"model": Message}},
    dependencies=_WRITE,
)
async def put_external_calendar(
    payload: ExternalCalendarIn, agent: CurrentAgent, session: DbSession
):
    _human(agent)
    cal = await calendar_import.set_external_calendar(
        session, agent_id=agent.id, ics_url=payload.ics_url
    )
    return _external_out(cal)


@router.delete(
    "/me/external-calendar",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect your calendar",
    description="Also removes every busy block it imported.",
    dependencies=_WRITE,
)
async def delete_external_calendar(agent: CurrentAgent, session: DbSession):
    _human(agent)
    await calendar_import.remove_external_calendar(session, agent_id=agent.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/external-calendar/sync",
    response_model=CalendarSyncOut,
    summary="Sync your calendar now",
    description="A feed that fails returns `status: ERROR` with the reason, and the "
                "busy time from the last good sync stays in place.",
    dependencies=_WRITE,
)
async def sync_external_calendar(agent: CurrentAgent, session: DbSession):
    _human(agent)
    return await calendar_import.sync_external_calendar(session, agent_id=agent.id)

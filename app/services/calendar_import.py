"""Busy time from an agent's own calendar (009) — the one thing that comes in.

The agent pastes their calendar's secret iCal address (Google Calendar →
Settings → Integrate calendar → "Secret address in iCal format"). Every busy
event in the next CALENDAR_IMPORT_WINDOW_DAYS becomes a core.agent_time_off row
with source='ICS', so /slots and the booking check subtract it with no new
logic. Only start and end are kept — never titles, attendees or descriptions:
the agent's private life is not CRM data.

Sync is on demand, not a job: `maybe_refresh` re-fetches when the last sync is
older than CALENDAR_SYNC_TTL_MINUTES, called from the routes that read a
calendar and before a booking takes its lock. It is a write on a read path,
accepted knowingly (docs/DECISIONS.md §19): the TTL, a hard fetch timeout, a
size cap and swallowing every error keep a dead feed from failing a request.

This module imports no other service, so anything may import it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from fastapi import HTTPException, status
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AgentExternalCalendar, AgentTimeOff

log = logging.getLogger(__name__)

# Every visit we export carries this UID suffix (services/calendar.py). An
# agent who copies one of our invites into their own calendar would otherwise
# import it straight back as busy time over the very visit it describes.
OWN_UID_SUFFIX = "@homelitics"

MAX_ICS_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class BusyBlock:
    external_uid: str      # event UID + occurrence start: an RRULE is many rows
    starts_at: datetime
    ends_at: datetime


# ------------------------------------------------------------------ the URL

def _host_allowed(host: str) -> bool:
    for entry in get_settings().calendar_import_host_list:
        if (entry.startswith(".") and host.endswith(entry)) or host == entry:
            return True
    return False


def normalize_url(raw: str) -> str:
    """https only (webcal:// is https in disguise), allow-listed hosts only.

    The API fetches whatever an agent pastes, so an open URL field would let
    anyone make it request internal addresses (SSRF). Redirects are re-checked
    against the same list when fetched.
    """
    url = raw.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The calendar address must be an https:// or webcal:// URL")
    host = parts.hostname.lower()
    if not _host_allowed(host):
        allowed = ", ".join(get_settings().calendar_import_host_list)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{host} is not an accepted calendar host ({allowed})")
    return url


def mask_url(url: str) -> str:
    """Enough to recognise the calendar, not enough to read it."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…{url[-10:]}"


async def _fetch_ics(url: str, *, timeout: float) -> bytes:
    async def _check_hop(request: httpx.Request) -> None:
        if request.url.scheme != "https" or not _host_allowed(request.url.host.lower()):
            raise ValueError(f"redirected to a non-accepted host: {request.url.host}")

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, max_redirects=3,
        event_hooks={"request": [_check_hop]},
    ) as client:
        async with client.stream("GET", url, headers={"Accept": "text/calendar"}) as r:
            if r.status_code != 200:
                raise ValueError(f"calendar host answered HTTP {r.status_code}")
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_ICS_BYTES:
                    raise ValueError("calendar is larger than 2 MB")
                chunks.append(chunk)
    return b"".join(chunks)


# ------------------------------------------------------------------ parsing

def _as_utc(value: date | datetime, tz: ZoneInfo) -> datetime:
    """All-day dates start at local midnight; floating times are local too."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time(0), tzinfo=tz)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc)


def parse_busy(
    ics: bytes, *, window_start: datetime, window_end: datetime, tz: ZoneInfo
) -> tuple[list[BusyBlock], int]:
    """Busy blocks overlapping [window_start, window_end), and how many events
    were skipped (cancelled, marked free, our own visits, or zero-length).

    Recurring events are expanded over the window by `recurring-ical-events`;
    DTEND is exclusive, so an all-day event on the 12th blocks exactly the
    12th in local time.
    """
    calendar = icalendar.Calendar.from_ical(ics)
    blocks: dict[str, BusyBlock] = {}
    skipped = 0
    for event in recurring_ical_events.of(calendar).between(window_start, window_end):
        uid = str(event.get("UID") or "")
        if (
            str(event.get("STATUS") or "").upper() == "CANCELLED"
            or str(event.get("TRANSP") or "").upper() == "TRANSPARENT"
            or uid.endswith(OWN_UID_SUFFIX)
        ):
            skipped += 1
            continue

        dtstart = event.get("DTSTART")
        if dtstart is None:
            skipped += 1
            continue
        start = _as_utc(dtstart.dt, tz)
        if event.get("DTEND") is not None:
            end = _as_utc(event.get("DTEND").dt, tz)
        elif event.get("DURATION") is not None:
            end = start + event.get("DURATION").dt
        elif not isinstance(dtstart.dt, datetime):
            end = start + timedelta(days=1)          # a bare all-day date
        else:
            end = start
        if end <= start:
            skipped += 1
            continue

        key = f"{uid or 'no-uid'}#{start.isoformat()}"
        blocks[key] = BusyBlock(external_uid=key, starts_at=start, ends_at=end)
    return list(blocks.values()), skipped


# ------------------------------------------------------------------ syncing

def _short(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:500]


async def sync_external_calendar(session: AsyncSession, *, agent_id: uuid.UUID) -> dict:
    """Fetch, parse and reconcile the agent's ICS time off. Commits.

    A bad feed never raises: the error is stamped on the calendar row and
    returned, and the busy time from the last good sync stays in place.
    MANUAL time off is never touched.
    """
    settings = get_settings()
    cal = await session.get(AgentExternalCalendar, agent_id)
    if cal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No external calendar connected")

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=settings.calendar_import_window_days)
    try:
        raw = await _fetch_ics(cal.ics_url, timeout=settings.calendar_fetch_timeout_s)
        blocks, skipped = parse_busy(
            raw, window_start=now, window_end=window_end, tz=ZoneInfo(settings.app_timezone)
        )
    except Exception as exc:  # noqa: BLE001 — any failure is the feed's, recorded not raised
        cal.last_synced_at, cal.last_status, cal.last_error = now, "ERROR", _short(exc)
        await session.commit()
        log.warning("calendar sync failed for agent %s: %s", agent_id, cal.last_error)
        return {"status": "ERROR", "imported": 0, "removed": 0, "skipped": 0,
                "error": cal.last_error}

    # One sync per agent at a time; a second concurrent one simply stands down.
    got_lock = await session.scalar(
        text("select pg_try_advisory_xact_lock(hashtextextended('sync:' || :aid, 0))"),
        {"aid": str(agent_id)},
    )
    if not got_lock:
        await session.commit()      # nothing written; ends the transaction without expiring rows
        return {"status": "OK", "imported": 0, "removed": 0, "skipped": skipped, "error": None}

    in_window = (
        AgentTimeOff.agent_id == agent_id,
        AgentTimeOff.source == "ICS",
        AgentTimeOff.ends_at > now,
        AgentTimeOff.starts_at < window_end,
    )
    known = set(
        (await session.execute(select(AgentTimeOff.external_uid).where(*in_window))).scalars()
    )
    fresh = {b.external_uid for b in blocks}

    if blocks:
        # One statement, not one round trip per event. Keys are unique already
        # (parse_busy dedups), which ON CONFLICT DO UPDATE requires.
        stmt = insert(AgentTimeOff).values([
            {"agent_id": agent_id, "starts_at": b.starts_at, "ends_at": b.ends_at,
             "reason": "External calendar", "source": "ICS", "external_uid": b.external_uid}
            for b in blocks
        ])
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AgentTimeOff.agent_id, AgentTimeOff.external_uid],
                # A literal, not a bind parameter: Postgres infers a partial
                # arbiter index (uq_agent_time_off_ics) only from a predicate
                # it can prove at plan time.
                index_where=text("source = 'ICS'"),
                set_={"starts_at": stmt.excluded.starts_at, "ends_at": stmt.excluded.ends_at},
            )
        )
    vanished = known - fresh
    if vanished:
        await session.execute(
            delete(AgentTimeOff).where(*in_window, AgentTimeOff.external_uid.in_(vanished))
        )

    cal.last_synced_at, cal.last_status, cal.last_error = now, "OK", None
    await session.commit()
    return {"status": "OK", "imported": len(fresh - known), "removed": len(vanished),
            "skipped": skipped, "error": None}


async def maybe_refresh(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """Re-sync if the agent connected a calendar and it is stale. Never raises.

    The sync runs in a session of its own: it commits (or rolls back) without
    touching the caller's transaction or expiring the rows the caller already
    loaded. Under READ COMMITTED the caller's next statement sees the new rows.
    """
    from app.db import get_sessionmaker  # lazy: app.db builds the engine from settings

    try:
        row = (
            await session.execute(
                select(AgentExternalCalendar.last_synced_at)
                .where(AgentExternalCalendar.agent_id == agent_id)
            )
        ).first()
        if row is None:
            return
        ttl = timedelta(minutes=get_settings().calendar_sync_ttl_minutes)
        if row.last_synced_at is not None and datetime.now(timezone.utc) - row.last_synced_at < ttl:
            return
        async with get_sessionmaker()() as own:
            await sync_external_calendar(own, agent_id=agent_id)
    except Exception:  # noqa: BLE001 — stale busy time beats a failed booking
        log.exception("calendar refresh failed for agent %s", agent_id)


# ------------------------------------------------------------------ the connection

async def get_external_calendar(
    session: AsyncSession, *, agent_id: uuid.UUID
) -> AgentExternalCalendar:
    cal = await session.get(AgentExternalCalendar, agent_id)
    if cal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No external calendar connected")
    return cal


async def set_external_calendar(
    session: AsyncSession, *, agent_id: uuid.UUID, ics_url: str
) -> AgentExternalCalendar:
    """Connect (or replace) the agent's calendar and sync it straight away, so
    the answer already says whether the address works."""
    url = normalize_url(ics_url)
    cal = await session.get(AgentExternalCalendar, agent_id)
    if cal is None:
        cal = AgentExternalCalendar(agent_id=agent_id, ics_url=url)
        session.add(cal)
    else:
        cal.ics_url = url
        cal.last_synced_at = cal.last_status = cal.last_error = None
    await session.commit()
    # A replaced calendar's old blocks vanish from the new feed and are removed.
    await sync_external_calendar(session, agent_id=agent_id)
    await session.refresh(cal)
    return cal


async def remove_external_calendar(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """Disconnect, and drop every block it imported."""
    cal = await get_external_calendar(session, agent_id=agent_id)
    await session.execute(
        delete(AgentTimeOff).where(
            AgentTimeOff.agent_id == agent_id, AgentTimeOff.source == "ICS"
        )
    )
    await session.delete(cal)
    await session.commit()

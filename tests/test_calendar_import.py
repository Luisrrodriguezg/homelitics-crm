"""Busy time imported from an agent's own calendar (009).

The fetch is swapped for a function returning a fixture feed, so these tests
exercise everything after the network: URL rules, parsing (TZID, all-day,
cancelled, free, RRULE, our own UIDs echoed back), reconciliation, and — the
point of it all — that imported busy time takes slots off the bot's hands.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from tests.conftest import get_new_session

BOGOTA = ZoneInfo("America/Bogota")
GOOGLE_URL = "https://calendar.google.com/calendar/ical/agent%40example.com/private-abc/basic.ics"


def _day(n: int) -> date:
    return datetime.now(BOGOTA).date() + timedelta(days=n)


def _feed(*, with_dentist: bool = True, extra: str = "") -> bytes:
    """A Google-style feed relative to today. Busy: the dentist (TZID), an
    all-day block, a weekly standup x3. Skipped: a cancelled event, a 'free'
    one, and a copy of one of our own visits."""
    d = lambda n: _day(n).strftime("%Y%m%d")                       # noqa: E731
    dentist = f"""BEGIN:VEVENT
UID:dentist@google.com
DTSTART;TZID=America/Bogota:{d(2)}T100000
DTEND;TZID=America/Bogota:{d(2)}T110000
SUMMARY:Dentist
END:VEVENT
""" if with_dentist else ""
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
{dentist}BEGIN:VEVENT
UID:offsite@google.com
DTSTART;VALUE=DATE:{d(3)}
DTEND;VALUE=DATE:{d(4)}
SUMMARY:Offsite
END:VEVENT
BEGIN:VEVENT
UID:standup@google.com
DTSTART;TZID=America/Bogota:{d(5)}T090000
DTEND;TZID=America/Bogota:{d(5)}T093000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:called-off@google.com
STATUS:CANCELLED
DTSTART;TZID=America/Bogota:{d(4)}T100000
DTEND;TZID=America/Bogota:{d(4)}T110000
END:VEVENT
BEGIN:VEVENT
UID:reminder@google.com
TRANSP:TRANSPARENT
DTSTART;TZID=America/Bogota:{d(4)}T120000
DTEND;TZID=America/Bogota:{d(4)}T130000
END:VEVENT
BEGIN:VEVENT
UID:4b1c0d9e-0000-0000-0000-000000000000@homelitics
DTSTART;TZID=America/Bogota:{d(4)}T140000
DTEND;TZID=America/Bogota:{d(4)}T150000
SUMMARY:Visita inmobiliaria
END:VEVENT
{extra}END:VCALENDAR
""".replace("\n", "\r\n").encode()


def _local(n: int, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(_day(n), time(hh, mm), tzinfo=BOGOTA).astimezone(timezone.utc)


# ------------------------------------------------------------------ parser (no DB)

def test_parse_busy_keeps_busy_time_and_skips_the_rest():
    from app.services.calendar_import import parse_busy

    now = datetime.now(timezone.utc)
    blocks, skipped = parse_busy(_feed(), window_start=now, window_end=now + timedelta(days=60),
                                 tz=BOGOTA)
    by_start = {b.starts_at: b for b in blocks}
    assert skipped == 3                                  # cancelled, free, our own visit
    assert len(blocks) == 1 + 1 + 3                      # dentist, offsite, standup x3
    dentist = by_start[_local(2, 10)]
    assert dentist.ends_at == _local(2, 11)
    assert dentist.external_uid.startswith("dentist@google.com#")
    offsite = by_start[_local(3, 0)]                     # all day = local midnight..midnight
    assert offsite.ends_at == _local(4, 0)
    standups = sorted(b.starts_at for b in blocks if b.external_uid.startswith("standup"))
    assert standups == [_local(5, 9), _local(12, 9), _local(19, 9)]


# ------------------------------------------------------------------ DB

@pytest.fixture
def feed(monkeypatch):
    """The feed the fake fetch serves; tests edit feed['ics'] or feed['fail']."""
    from app.services import calendar_import

    state = {"ics": _feed(), "fail": None, "fetched": 0}

    async def _fake_fetch(url, *, timeout):
        state["fetched"] += 1
        if state["fail"]:
            raise state["fail"]
        return state["ics"]

    monkeypatch.setattr(calendar_import, "_fetch_ics", _fake_fetch)
    return state


async def _connect(c, url=GOOGLE_URL):
    return await c.put("/me/external-calendar", json={"ics_url": url})


async def _slots(c, agent_id, day, minutes=60):
    r = await c.get(f"/agents/{agent_id}/slots", params={
        "from": _local(day, 0).isoformat(), "to": _local(day + 1, 0).isoformat(),
        "duration_min": minutes,
    })
    assert r.status_code == 200, r.text
    return [datetime.fromisoformat(s) for s in r.json()["slots"]]


async def test_connecting_imports_busy_time_that_the_bot_cannot_book(world, client_for, feed):
    from tests.test_visit_rules import _book, _lead, _open_hours

    owner = world.agents[0][0]
    await _open_hours(owner.id)
    c, bot = client_for(owner), client_for(world.bots[0])
    assert _local(2, 10) in await _slots(c, owner.id, 2)            # free before

    r = await _connect(c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_status"] == "OK"
    assert "private-abc" not in body["ics_url_masked"]              # a credential, masked

    offs = (await c.get(f"/agents/{owner.id}/time-off")).json()
    assert sorted(o["source"] for o in offs) == ["ICS"] * 5
    assert all(o["reason"] == "External calendar" for o in offs)    # never the titles

    free = await _slots(c, owner.id, 2)
    assert _local(2, 10) not in free and _local(2, 9, 30) not in free   # 60-min visits
    assert _local(2, 9) in free and _local(2, 11) in free               # touching is fine
    assert await _slots(c, owner.id, 3) == []                           # the all-day block

    lead = await _lead(bot, world)
    r = await _book(bot, lead, _local(2, 10))
    assert r.status_code == 409 and "published availability" in r.json()["detail"]


async def test_resync_is_idempotent_and_leaves_manual_time_off_alone(world, client_for, feed):
    owner = world.agents[0][0]
    c = client_for(owner)
    r = await c.post(f"/agents/{owner.id}/time-off", json={
        "starts_at": _local(8, 9).isoformat(), "ends_at": _local(8, 10).isoformat(),
        "reason": "vacation",
    })
    assert r.status_code == 201
    await _connect(c)

    again = (await c.post("/me/external-calendar/sync")).json()
    assert again == {"status": "OK", "imported": 0, "removed": 0, "skipped": 3, "error": None}
    offs = (await c.get(f"/agents/{owner.id}/time-off")).json()
    assert sorted(o["source"] for o in offs) == ["ICS"] * 5 + ["MANUAL"]


async def test_an_event_removed_upstream_is_removed_here(world, client_for, feed):
    owner = world.agents[0][0]
    c = client_for(owner)
    await _connect(c)

    feed["ics"] = _feed(with_dentist=False)
    result = (await c.post("/me/external-calendar/sync")).json()
    assert result["removed"] == 1 and result["imported"] == 0
    offs = (await c.get(f"/agents/{owner.id}/time-off")).json()
    assert _local(2, 10) not in [datetime.fromisoformat(o["starts_at"]) for o in offs]


async def test_imported_blocks_are_edited_upstream_only(world, client_for, feed):
    owner = world.agents[0][0]
    c = client_for(owner)
    await _connect(c)
    off = (await c.get(f"/agents/{owner.id}/time-off")).json()[0]
    r = await c.delete(f"/agents/{owner.id}/time-off/{off['id']}")
    assert r.status_code == 409

    assert (await c.delete("/me/external-calendar")).status_code == 204
    assert (await c.get(f"/agents/{owner.id}/time-off")).json() == []
    assert (await c.get("/me/external-calendar")).status_code == 404


async def test_a_dead_feed_is_recorded_never_raised(world, client_for, feed):
    owner = world.agents[0][0]
    c = client_for(owner)
    await _connect(c)

    feed["fail"] = ConnectionError("calendar.google.com unreachable")
    r = await c.post("/me/external-calendar/sync")
    assert r.status_code == 200 and r.json()["status"] == "ERROR"
    assert "unreachable" in r.json()["error"]

    # a stale calendar is refreshed on read; a failure there must not fail the read
    async with get_new_session() as s:
        await s.execute(text(
            "update core.agent_external_calendar set last_synced_at = now() - interval '1 hour' "
            "where agent_id = :id"), {"id": owner.id})
        await s.commit()
    fetched = feed["fetched"]
    assert (await c.get(f"/agents/{owner.id}/slots", params={
        "from": _local(2, 0).isoformat(), "to": _local(3, 0).isoformat()})).status_code == 200
    assert feed["fetched"] == fetched + 1
    assert (await c.get("/me/external-calendar")).json()["last_status"] == "ERROR"
    # the busy time from the last good sync is still there
    assert len((await c.get(f"/agents/{owner.id}/time-off")).json()) == 5


async def test_a_refresh_that_blows_up_never_breaks_a_booking(world, client_for, feed,
                                                               monkeypatch):
    """The refresh runs in its own session. If it shared the booking's, a
    failure there would roll back and expire the lead the booking already
    loaded — and the next attribute read would crash the request."""
    from app.services import calendar_import
    from tests.test_visit_rules import _book, _lead, _open_hours

    owner = world.agents[0][0]
    await _open_hours(owner.id)
    c, bot = client_for(owner), client_for(world.bots[0])
    await _connect(c)
    async with get_new_session() as s:            # stale, so the booking refreshes
        await s.execute(text(
            "update core.agent_external_calendar set last_synced_at = now() - interval '1 hour' "
            "where agent_id = :id"), {"id": owner.id})
        await s.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("database hiccup mid-sync")
    monkeypatch.setattr(calendar_import, "sync_external_calendar", _boom)

    lead = await _lead(bot, world)
    r = await _book(bot, lead, _local(4, 10))     # a free day in the fixture feed
    assert r.status_code == 201, r.text


async def test_imported_busy_time_flags_a_visit_it_overlaps(world, client_for, feed):
    from tests.test_visit_rules import _book, _lead

    owner = world.agents[0][0]
    c = client_for(owner)
    lead = await _lead(c, world)
    appt = (await _book(c, lead, _local(6, 14))).json()      # booked before the conflict
    feed["ics"] = _feed(extra=f"""BEGIN:VEVENT
UID:school-run@google.com
DTSTART;TZID=America/Bogota:{_day(6):%Y%m%d}T143000
DTEND;TZID=America/Bogota:{_day(6):%Y%m%d}T160000
END:VEVENT
""")
    await _connect(c)

    events = (await c.get(f"/agents/{owner.id}/calendar", params={
        "from": _local(6, 0).isoformat(), "to": _local(7, 0).isoformat()})).json()["events"]
    visit = next(e for e in events if e["kind"] == "VISIT")
    assert visit["id"] == appt["id"] and visit["conflict"] is True


@pytest.mark.parametrize("url", [
    "http://calendar.google.com/calendar/ical/x/basic.ics",           # not https
    "https://evil.example.com/basic.ics",                             # not an accepted host
    "https://calendar.google.com.evil.example/basic.ics",             # suffix trick
])
async def test_only_https_addresses_on_accepted_hosts(world, client_for, feed, url):
    c = client_for(world.agents[0][0])
    r = await _connect(c, url)
    assert r.status_code == 422
    assert feed["fetched"] == 0


async def test_webcal_is_https_and_bots_cannot_connect(world, client_for, feed):
    c = client_for(world.agents[0][0])
    r = await _connect(c, GOOGLE_URL.replace("https://", "webcal://"))
    assert r.status_code == 200 and r.json()["ics_url_masked"].startswith("https://")

    bot = client_for(world.bots[0])
    assert (await _connect(bot)).status_code == 403

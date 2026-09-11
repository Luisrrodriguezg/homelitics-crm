"""Views of the visit calendar (009): the frontend's JSON, an agent's .ics feed,
and the single-visit .ics the bot hands a client."""
from __future__ import annotations

from datetime import datetime, time
from urllib.parse import urlsplit

from icalendar import Calendar
from sqlalchemy import select

from app.models import Agent
from tests.conftest import get_new_session
from tests.test_visit_rules import _at, _book, _lead, _open_hours


def _window(days_from=2, days_to=6):
    return {"from": _at(days_from, 0).isoformat(), "to": _at(days_to, 0).isoformat()}


def _anonymous():
    """What a calendar app is: no Authorization header, no agent header."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def _vevents(body: bytes):
    return Calendar.from_ical(body).walk("VEVENT")


# ------------------------------------------------------------------ JSON

async def test_agent_calendar_shows_visits_time_off_and_hours(world, client_for):
    owner = world.agents[0][0]
    await _open_hours(owner.id, start=time(9), end=time(12))
    c = client_for(owner)
    lead = await _lead(c, world)
    appt = (await _book(c, lead, _at(3, 10))).json()
    r = await c.post(f"/agents/{owner.id}/time-off", json={
        "starts_at": _at(4, 9).isoformat(), "ends_at": _at(4, 10).isoformat(),
        "reason": "dentist",
    })
    assert r.status_code == 201, r.text

    r = await c.get(f"/agents/{owner.id}/calendar", params=_window())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timezone"] == "America/Bogota"
    kinds = {e["kind"] for e in body["events"]}
    assert kinds == {"VISIT", "TIME_OFF", "AVAILABILITY"}

    visit = next(e for e in body["events"] if e["kind"] == "VISIT")
    assert visit["id"] == appt["id"] and visit["status"] == "CONFIRMED"
    assert visit["location"].startswith("1 Test St, N, ")
    assert visit["title"].startswith("Visita · ") and visit["title"].endswith("· N")
    assert visit["booked_by_bot"] is False and visit["conflict"] is False
    off = next(e for e in body["events"] if e["kind"] == "TIME_OFF")
    assert off["title"] == "dentist" and off["source"] == "MANUAL"


async def test_calendar_is_agency_scoped(world, client_for):
    mine, theirs = world.agents[0][0], world.agents[1][0]
    c = client_for(mine)
    lead = await _lead(c, world)
    await _book(c, lead, _at(3, 10))

    other = client_for(theirs)
    assert (await other.get(f"/agents/{mine.id}/calendar", params=_window())).status_code == 404
    r = await other.get("/calendar", params=_window())
    assert r.status_code == 200 and r.json()["events"] == []

    team = (await c.get("/calendar", params=_window())).json()["events"]
    assert [e["agent_name"] for e in team if e["kind"] == "VISIT"] == [f"{world.tag} agent 0.0"]


async def test_calendar_window_is_capped(world, client_for):
    c = client_for(world.agents[0][0])
    r = await c.get("/calendar", params={"from": _at(0, 0).isoformat(),
                                         "to": _at(63, 0).isoformat()})
    assert r.status_code == 422


# ------------------------------------------------------------------ the feed

async def test_feed_is_a_token_url_calendar_apps_can_fetch(world, client_for):
    owner = world.agents[0][0]
    c = client_for(owner)
    lead = await _lead(c, world)
    appt = (await _book(c, lead, _at(3, 10))).json()

    urls = (await c.get("/me/calendar-feed")).json()
    assert urls["webcal_url"].startswith("webcal://")
    async with _anonymous() as app_client:
        r = await app_client.get(_path(urls["ics_url"]))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/calendar")
        [event] = _vevents(r.content)
        assert str(event["UID"]) == f"{appt['id']}@homelitics"
        assert str(event["STATUS"]) == "CONFIRMED"
        assert event["DTSTART"].dt == datetime.fromisoformat(appt["scheduled_at"])

        # a cancelled visit simply leaves the feed
        await c.patch(f"/appointments/{appt['id']}", json={"status": "CANCELLED"})
        r = await app_client.get(_path(urls["ics_url"]))
        assert _vevents(r.content) == []

        # a wrong token, or a bot row's own token, confirms nothing
        bad = urls["ics_url"].rsplit("=", 1)[0] + "=00000000-0000-0000-0000-000000000000"
        assert (await app_client.get(_path(bad))).status_code == 404
        bot = world.bots[0]
        async with get_new_session() as s:     # a server default the fixture never read
            bot_token = await s.scalar(select(Agent.calendar_token).where(Agent.id == bot.id))
        r = await app_client.get(f"/agents/{bot.id}/calendar.ics",
                                 params={"token": str(bot_token)})
        assert r.status_code == 404

        # rotating kills the old URL
        new = (await c.post("/me/calendar-feed/rotate")).json()
        assert new["ics_url"] != urls["ics_url"]
        assert (await app_client.get(_path(urls["ics_url"]))).status_code == 404
        assert (await app_client.get(_path(new["ics_url"]))).status_code == 200


async def test_bots_have_no_feed(world, client_for):
    bot = client_for(world.bots[0])
    assert (await bot.get("/me/calendar-feed")).status_code == 404
    assert (await bot.post("/me/calendar-feed/rotate")).status_code == 403


# ------------------------------------------------------------------ the client's copy

async def test_invite_and_google_link_carry_nothing_about_the_client(world, client_for):
    await _open_hours(world.agents[0][0].id)
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world)
    appt = (await _book(bot, lead, _at(3, 10))).json()

    r = await bot.get(f"/appointments/{appt['id']}/invite.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    client_name = f"{world.tag} client 0"
    assert client_name.encode() not in r.content
    [event] = _vevents(r.content)
    assert str(event["SUMMARY"]) == "Visita inmobiliaria · N"
    assert str(event["STATUS"]) == "TENTATIVE"            # pending the owner's yes
    assert str(event["UID"]) == f"{appt['id']}@homelitics"

    detail = (await bot.get(f"/appointments/{appt['id']}")).json()
    assert detail["google_calendar_url"].startswith(
        "https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert client_name not in detail["google_calendar_url"]
    assert detail["location"].startswith("1 Test St")
    assert detail["agent_name"] == f"{world.tag} agent 0.0"

"""Service accounts: an AI agent across every agency, with guardrails (007).

The bot is a core.service_account whose token resolves, per request, to one of
its per-agency AI_AGENT rows. Covered here: that resolution through the REAL
get_current_agent (only signature checking stubbed), tenancy, ownership,
scopes, the write budget, and the metric quarantine — the last one is the
reason the whole design exists.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text, update

from tests.conftest import get_new_session


async def _lead(c, world, listing_idx=0, client_idx=0, **extra):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[listing_idx].id),
        "source_channel": "TELEGRAM", **extra,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _set_account(world, **values):
    from app.models import ServiceAccount
    async with get_new_session() as s:
        await s.execute(
            update(ServiceAccount).where(ServiceAccount.id == world.service_account_id).values(**values)
        )
        await s.commit()


async def _outcome(lead_id):
    async with get_new_session() as s:
        row = (await s.execute(
            text("select first_outbound, has_outbound from analytics.lead_outcome where lead_id = :id"),
            {"id": lead_id},
        )).one()
    return row


# ------------------------------------------------- resolution (real deps path)

@pytest_asyncio.fixture
async def raw_client(monkeypatch):
    """A client that goes through the REAL get_current_agent.

    Only `decode_token` is stubbed — the thing under test is how a `sub` resolves
    to an agent, not JWT signature checking (that is app/auth.py's job).
    """
    from httpx import ASGITransport, AsyncClient
    from app import deps
    from app.main import app

    app.dependency_overrides.pop(deps.get_current_agent, None)
    opened = []

    def _make(sub, agency_id=None):
        monkeypatch.setattr(
            deps, "decode_token",
            lambda token, settings=None: SimpleNamespace(sub=str(sub), email=None, role=None, raw={}),
        )
        headers = {"Authorization": "Bearer stub"}
        if agency_id is not None:
            headers["X-Agency-Id"] = str(agency_id)
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers=headers, timeout=30.0)
        opened.append(c)
        return c

    yield _make
    for c in opened:
        await c.aclose()


async def test_service_account_resolves_to_that_agencys_bot_row(world, raw_client):
    c = raw_client(world.service_account.auth_user_id, world.agencies[0].id)
    r = await c.get("/me")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["role"] == "AI_AGENT"
    assert me["agency_id"] == str(world.agencies[0].id)
    assert me["id"] == str(world.bots[0].id)


async def test_same_token_other_header_is_the_other_agency(world, raw_client):
    c = raw_client(world.service_account.auth_user_id, world.agencies[1].id)
    me = (await c.get("/me")).json()
    assert me["agency_id"] == str(world.agencies[1].id)
    assert me["id"] == str(world.bots[1].id)


async def test_missing_or_malformed_agency_header_is_400(world, raw_client):
    sub = world.service_account.auth_user_id
    assert (await raw_client(sub).get("/me")).status_code == 400
    assert (await raw_client(sub, "not-a-uuid").get("/me")).status_code == 400


async def test_agency_without_a_bot_row_is_403(world, raw_client):
    c = raw_client(world.service_account.auth_user_id, uuid.uuid4())
    assert (await c.get("/me")).status_code == 403


async def test_human_token_ignores_the_header(world, raw_client):
    """X-Agency-Id is a service-account concept; a human cannot hop agencies with it."""
    human = world.agents[0][0]
    c = raw_client(human.auth_user_id, world.agencies[1].id)
    me = (await c.get("/me")).json()
    assert me["agency_id"] == str(world.agencies[0].id)
    assert me["role"] == "TEAM_ADMIN"


async def test_unknown_sub_is_still_403(world, raw_client):
    c = raw_client(uuid.uuid4(), world.agencies[0].id)
    assert (await c.get("/me")).status_code == 403


async def test_kill_switch_on_the_account(world, raw_client):
    await _set_account(world, active=False)
    c = raw_client(world.service_account.auth_user_id, world.agencies[0].id)
    r = await c.get("/me")
    assert r.status_code == 403
    assert "deactivated" in r.json()["detail"].lower()


async def test_kill_switch_on_one_agency(world, raw_client):
    from app.models import Agent
    async with get_new_session() as s:
        await s.execute(update(Agent).where(Agent.id == world.bots[1].id).values(active=False))
        await s.commit()
    sub = world.service_account.auth_user_id
    assert (await raw_client(sub, world.agencies[1].id).get("/me")).status_code == 403
    assert (await raw_client(sub, world.agencies[0].id).get("/me")).status_code == 200


# ---------------------------------------------------- tenancy and ownership

async def test_bot_lead_is_owned_by_the_listing_agent_and_attributed(world, client_for):
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world, message="hola, me interesa")
    assert lead["agent_id"] == str(world.listings[0].agent_id)   # not the bot

    history = (await bot.get(f"/leads/{lead['id']}/transitions")).json()
    assert history[0]["to_stage"] == "INTERESTED"
    assert history[0]["changed_by"] == str(world.bots[0].id)

    timeline = (await bot.get(f"/leads/{lead['id']}/interactions")).json()
    assert timeline[0]["direction"] == "INBOUND"
    assert timeline[0]["created_by"] == str(world.bots[0].id)


async def test_bot_row_is_confined_to_its_agency(world, client_for):
    lead = await _lead(client_for(world.bots[0]), world)
    other = client_for(world.bots[1])
    assert (await other.get(f"/leads/{lead['id']}")).status_code == 404
    r = await other.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),   # agency 0's listing
        "source_channel": "CALL",
    })
    assert r.status_code == 404


async def test_admin_cannot_reassign_a_lead_to_the_bot(world, client_for):
    admin = client_for(world.agents[0][0])
    lead = await _lead(admin, world)
    r = await admin.post(f"/leads/{lead['id']}/reassign", json={"to_agent_id": str(world.bots[0].id)})
    assert r.status_code == 409, r.text
    assert "ai agent" in r.json()["detail"].lower()


# --------------------------------------------------------------------- scopes

async def test_default_scopes_allow_forward_moves_but_not_closing(world, client_for):
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world)
    lid = lead["id"]

    r = await bot.post(f"/leads/{lid}/transitions", json={"to_stage": "VISIT_SCHEDULED"})
    assert r.status_code == 201, r.text

    r = await bot.post(f"/leads/{lid}/transitions",
                       json={"to_stage": "LOST", "lost_reason": "NO_RESPONSE"})
    assert r.status_code == 403, r.text
    assert "leads:close" in r.json()["detail"]
    assert (await bot.get(f"/leads/{lid}")).json()["current_stage"] == "VISIT_SCHEDULED"

    await _set_account(world, scopes=world.service_account.scopes + ["leads:close"])
    r = await bot.post(f"/leads/{lid}/transitions",
                       json={"to_stage": "LOST", "lost_reason": "NO_RESPONSE"})
    assert r.status_code == 201, r.text


async def test_ungranted_routes_are_403_for_the_bot(world, client_for):
    bot = client_for(world.bots[0])
    lead = await _lead(bot, world)
    human_agent = world.agents[0][1]

    r = await bot.post(f"/leads/{lead['id']}/reassign",
                       json={"to_agent_id": str(human_agent.id)})
    assert r.status_code == 403                                   # TEAM_ADMIN only

    r = await bot.post(f"/agents/{human_agent.id}/availability",
                       json={"weekday": 0, "start_time": "09:00", "end_time": "12:00"})
    assert r.status_code == 403 and "availability:write" in r.json()["detail"]

    r = await bot.post(f"/listings/{world.listings[0].id}/views", json={"session_id": "s"})
    assert r.status_code == 403 and "listings:views" in r.json()["detail"]

    r = await bot.patch(f"/appointments/{uuid.uuid4()}", json={"status": "CONFIRMED"})
    assert r.status_code == 403 and "visits:manage" in r.json()["detail"]


async def test_humans_are_never_scope_checked(world, client_for):
    """The scope layer must be invisible to people: an AGENT with none of the
    bot's scopes can still do everything it could before 007."""
    human = client_for(world.agents[0][1])
    lead = await _lead(human, world)
    r = await human.post(f"/leads/{lead['id']}/interactions",
                         json={"direction": "OUTBOUND", "channel": "TELEGRAM", "body": "hi"})
    assert r.status_code == 201
    r = await human.post(f"/listings/{world.listings[0].id}/views", json={"session_id": "s"})
    assert r.status_code == 201


# --------------------------------------------------------------------- budget

async def test_hourly_write_budget_returns_429(world, client_for):
    human = client_for(world.agents[0][1])
    lead = await _lead(human, world)
    await _set_account(world, hourly_write_limit=2)

    bot = client_for(world.bots[0])
    body = {"direction": "OUTBOUND", "channel": "TELEGRAM", "body": "auto-reply"}
    lid = lead["id"]
    assert (await bot.post(f"/leads/{lid}/interactions", json=body)).status_code == 201
    assert (await bot.post(f"/leads/{lid}/interactions", json=body)).status_code == 201
    r = await bot.post(f"/leads/{lid}/interactions", json=body)
    assert r.status_code == 429, r.text
    assert r.headers.get("retry-after") == "3600"

    # Reads are never budgeted, and humans are unaffected.
    assert (await bot.get(f"/leads/{lid}")).status_code == 200
    assert (await human.post(f"/leads/{lid}/interactions", json=body)).status_code == 201


# -------------------------------------------------------------------- metrics

async def test_bot_and_internal_notes_do_not_stop_the_response_clock(world, client_for):
    """The reason 007 exists. Only a human's OUTBOUND MESSAGE/CALL is a response;
    a bot reply, a bot stage-change note and a human NOTE all leave the lead
    unanswered in analytics.lead_outcome."""
    human = client_for(world.agents[0][1])
    bot = client_for(world.bots[0])
    lead = await _lead(human, world, message="interested")
    lid = lead["id"]

    r = await bot.post(f"/leads/{lid}/transitions",
                       json={"to_stage": "VISIT_SCHEDULED", "note": "auto-scheduled"})
    assert r.status_code == 201, r.text                     # writes an OUTBOUND STATUS_CHANGE
    r = await bot.post(f"/leads/{lid}/interactions",
                       json={"direction": "OUTBOUND", "channel": "TELEGRAM", "body": "bot reply"})
    assert r.status_code == 201, r.text
    r = await human.post(f"/leads/{lid}/interactions",
                         json={"direction": "OUTBOUND", "channel": "IN_APP",
                               "type": "NOTE", "body": "internal note"})
    assert r.status_code == 201, r.text

    first_outbound, has_outbound = await _outcome(lid)
    assert first_outbound is None and has_outbound is False

    r = await human.post(f"/leads/{lid}/interactions",
                         json={"direction": "OUTBOUND", "channel": "TELEGRAM", "body": "real reply"})
    assert r.status_code == 201
    first_outbound, has_outbound = await _outcome(lid)
    assert first_outbound is not None and has_outbound is True

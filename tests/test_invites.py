"""Invites (012): the only way to sign up. The agency and role come from the
invite, never from the person registering; one token makes one agent."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.models import Agent, AgentInvite, DomainEvent
from app.services import auth_admin


@pytest_asyncio.fixture
async def anon():
    """A client with no identity at all: the invitee has no account yet."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           timeout=30.0) as c:
        yield c


@pytest.fixture
def fake_auth(monkeypatch):
    """Stand-in for the Supabase Admin API. Records who was created/deleted."""
    calls = {"created": [], "deleted": []}

    async def _create(settings, *, email, password, metadata):
        await asyncio.sleep(0.05)   # long enough for a concurrent redeem to queue
        calls["created"].append((email, metadata))
        return uuid.uuid4()

    async def _delete(settings, user_id):
        calls["deleted"].append(user_id)

    monkeypatch.setattr(auth_admin, "create_user", _create)
    monkeypatch.setattr(auth_admin, "delete_user", _delete)
    return calls


async def _invite(world, client_for, **body):
    r = await client_for(world.agents[0][0]).post("/invites", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _signup(world, token, **over):
    body = {"token": token, "full_name": f"{world.tag} newcomer",
            "email": f"{uuid.uuid4().hex[:8]}@example.test", "password": "s3cret-pass"}
    return {**body, **over}


async def test_admin_invites_and_list_hides_token(world, client_for):
    inv = await _invite(world, client_for)
    assert inv["role"] == "AGENT" and inv["status"] == "PENDING" and inv["token"]
    listed = (await client_for(world.agents[0][0]).get("/invites")).json()
    assert [i["id"] for i in listed] == [inv["id"]]
    assert "token" not in listed[0]
    # another agency's admin sees none of it
    assert (await client_for(world.agents[1][0]).get("/invites")).json() == []


async def test_only_team_admins_manage_invites(world, client_for):
    for who in (world.agents[0][1], world.bots[0]):   # a plain AGENT, an AI agent
        c = client_for(who)
        assert (await c.post("/invites", json={})).status_code == 403
        assert (await c.get("/invites")).status_code == 403


async def test_accept_joins_the_invites_agency_with_its_role(world, client_for, anon,
                                                             fake_auth, session):
    inv = await _invite(world, client_for)
    other_agency = str(world.agencies[1].id)
    # Smuggled fields are ignored: the invite decides agency and role.
    r = await anon.post("/invites/accept", json={
        **_signup(world, inv["token"], email="New.Person@Example.test"),
        "role": "TEAM_ADMIN", "agency_id": other_agency,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "AGENT"
    assert body["agency_id"] == str(world.agencies[0].id)
    assert body["email"] == "new.person@example.test"
    assert fake_auth["created"][0][0] == "new.person@example.test"

    agent = await session.scalar(select(Agent).where(Agent.id == uuid.UUID(body["id"])))
    assert agent.auth_user_id is not None, "the login was not bound to the agent"
    listed = (await client_for(world.agents[0][0]).get("/invites")).json()
    assert listed[0]["status"] == "REDEEMED" and listed[0]["redeemed_agent_id"] == body["id"]
    joined = await session.scalar(select(DomainEvent).where(
        DomainEvent.event_type == "agent.joined", DomainEvent.aggregate_id == agent.id))
    assert joined is not None and joined.payload["role"] == "AGENT"
    assert joined.actor_agent_id == agent.id

    again = await anon.post("/invites/accept", json=_signup(world, inv["token"]))
    assert again.status_code == 410


async def test_admin_invite_makes_an_admin_who_can_invite(world, client_for, anon, fake_auth,
                                                         session):
    inv = await _invite(world, client_for, role="TEAM_ADMIN")
    r = await anon.post("/invites/accept", json=_signup(world, inv["token"]))
    assert r.status_code == 201 and r.json()["role"] == "TEAM_ADMIN"
    new_admin = await session.scalar(select(Agent).where(Agent.id == uuid.UUID(r.json()["id"])))
    assert (await client_for(new_admin).post("/invites", json={})).status_code == 201


async def test_email_lock(world, client_for, anon, fake_auth):
    inv = await _invite(world, client_for, email="Locked@Example.test")
    assert inv["email"] == "locked@example.test"
    wrong = await anon.post("/invites/accept", json=_signup(world, inv["token"]))
    assert wrong.status_code == 403
    right = await anon.post("/invites/accept",
                            json=_signup(world, inv["token"], email="LOCKED@example.test"))
    assert right.status_code == 201, right.text


async def test_unknown_expired_and_revoked_tokens(world, client_for, anon, fake_auth, session):
    assert (await anon.post("/invites/accept",
                            json=_signup(world, "x" * 43))).status_code == 404

    expired = await _invite(world, client_for)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    await session.execute(update(AgentInvite).where(AgentInvite.id == uuid.UUID(expired["id"]))
                          .values(created_at=past, expires_at=past + timedelta(hours=1)))
    await session.commit()
    assert (await anon.post("/invites/accept",
                            json=_signup(world, expired["token"]))).status_code == 410

    revoked = await _invite(world, client_for)
    admin = client_for(world.agents[0][0])
    assert (await admin.delete(f"/invites/{revoked['id']}")).status_code == 204
    assert (await admin.delete(f"/invites/{revoked['id']}")).status_code == 204  # idempotent
    assert (await anon.post("/invites/accept",
                            json=_signup(world, revoked["token"]))).status_code == 410
    assert not fake_auth["created"], "a dead invite reached Supabase"


async def test_revoke_is_agency_scoped_and_refuses_redeemed(world, client_for, anon, fake_auth):
    inv = await _invite(world, client_for)
    other_admin = client_for(world.agents[1][0])
    assert (await other_admin.delete(f"/invites/{inv['id']}")).status_code == 404
    assert (await anon.post("/invites/accept",
                            json=_signup(world, inv["token"]))).status_code == 201
    assert (await client_for(world.agents[0][0])
            .delete(f"/invites/{inv['id']}")).status_code == 409


async def test_email_taken_writes_nothing(world, client_for, anon, monkeypatch, session):
    async def _taken(settings, *, email, password, metadata):
        raise auth_admin.EmailTaken(email)
    monkeypatch.setattr(auth_admin, "create_user", _taken)

    inv = await _invite(world, client_for)
    r = await anon.post("/invites/accept", json=_signup(world, inv["token"]))
    assert r.status_code == 409
    n = await session.scalar(select(func.count()).select_from(Agent)
                             .where(Agent.agency_id == world.agencies[0].id))
    assert n == 3, "an agent row survived the failed sign-up"   # 2 people + the bot
    listed = (await client_for(world.agents[0][0]).get("/invites")).json()
    assert listed[0]["status"] == "PENDING", "the invite was burnt by a failed sign-up"


async def test_no_service_role_key_is_503(world, client_for, anon):
    # The real auth_admin, with SUPABASE_SERVICE_ROLE_KEY unset in the test env.
    inv = await _invite(world, client_for)
    r = await anon.post("/invites/accept", json=_signup(world, inv["token"]))
    assert r.status_code == 503, r.text


async def test_one_token_one_agent_under_concurrency(world, client_for, anon, fake_auth):
    inv = await _invite(world, client_for)
    a, b = await asyncio.gather(
        anon.post("/invites/accept", json=_signup(world, inv["token"])),
        anon.post("/invites/accept", json=_signup(world, inv["token"])),
    )
    assert sorted([a.status_code, b.status_code]) == [201, 410], (a.text, b.text)
    assert len(fake_auth["created"]) == 1


async def test_inspect_and_invite_url(world, client_for, anon, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "frontend_url", "https://app.example.test/")
    inv = await _invite(world, client_for, role="TEAM_ADMIN", expires_in_hours=24)
    assert inv["invite_url"] == f"https://app.example.test/invite#{inv['token']}"

    r = await anon.post("/invites/inspect", json={"token": inv["token"]})
    assert r.status_code == 200, r.text
    assert r.json()["agency_name"] == world.agencies[0].name
    assert r.json()["role"] == "TEAM_ADMIN"

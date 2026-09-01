"""Test fixtures.

These are integration tests: they run against the real Supabase database,
because the four things being tested (a UNIQUE race, a row lock, cross-tenant
filtering, a database trigger) are all database behaviour. A mock would prove
nothing about any of them.

Auth is stubbed via dependency_overrides so the tests do not need live Supabase
tokens — the thing under test is authorization (agency scoping), not
authentication (signature checking), and those are separate concerns.

Every test builds and tears down its own two-agency fixture, so it does not care
whether the database is empty or fully seeded.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

REQUIRED = ("DATABASE_URL", "SUPABASE_PROJECT_REF")
_missing = [v for v in REQUIRED if not os.environ.get(v)]


def pytest_collection_modifyitems(config, items):
    if _missing:
        skip = pytest.mark.skip(
            reason=f"needs a live database; set {', '.join(_missing)} in .env"
        )
        for item in items:
            item.add_marker(skip)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine_per_test():
    """Dispose the engine after every test.

    pytest-asyncio gives each test its own event loop, but app.db caches the
    engine with lru_cache. Without this, test 2 borrows a pooled asyncpg
    connection that was opened on test 1's loop and everything downstream dies
    with "attached to a different loop". Autouse and declared first, so it tears
    down last -- after the fixtures that still need a working session.
    """
    yield
    from app.db import get_engine, get_sessionmaker
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest_asyncio.fixture
async def session():
    from app.db import get_sessionmaker
    async with get_sessionmaker()() as s:
        yield s


@pytest_asyncio.fixture
async def world(session):
    """Two agencies, two agents each, one listing per agency, two clients.

    Yields a namespace and deletes everything it created afterwards, in FK order.
    """
    from app.models import (
        Agency, Agent, Client, Listing, Owner, Person, Property,
    )

    tag = f"pytest-{uuid.uuid4().hex[:8]}"
    made: dict = {"tag": tag}

    def person(name):
        p = Person(full_name=f"{tag} {name}")
        session.add(p)
        return p

    agencies, agents, listings = [], [], []
    for a_i in range(2):
        agency = Agency(name=f"{tag} agency {a_i}")
        session.add(agency)
        await session.flush()
        agencies.append(agency)

        pair = []
        for g_i in range(2):
            per = person(f"agent {a_i}.{g_i}")
            await session.flush()
            ag = Agent(
                person_id=per.id, agency_id=agency.id,
                role="TEAM_ADMIN" if g_i == 0 else "AGENT",
                auth_user_id=uuid.uuid4(),
            )
            session.add(ag)
            await session.flush()
            pair.append(ag)
        agents.append(pair)

        op = person(f"owner {a_i}")
        await session.flush()
        owner = Owner(person_id=op.id)
        session.add(owner)
        await session.flush()
        prop = Property(
            owner_id=owner.id, property_type="HOUSE", city=f"{tag} City",
            neighborhood="N", address="1 Test St", area_m2=100, bedrooms=3, bathrooms=2,
        )
        session.add(prop)
        await session.flush()
        listing = Listing(
            property_id=prop.id, agent_id=pair[0].id, operation_type="SALE",
            asking_price=100000, min_acceptable_price=90000,
        )
        session.add(listing)
        await session.flush()
        listings.append(listing)

    clients = []
    for c_i in range(2):
        cp = person(f"client {c_i}")
        await session.flush()
        c = Client(person_id=cp.id)
        session.add(c)
        await session.flush()
        clients.append(c)

    await session.commit()

    made.update(agencies=agencies, agents=agents, listings=listings, clients=clients)
    yield type("World", (), made)

    # ---- teardown, children first ----
    from sqlalchemy import delete, select
    from app.models import (
        AssignmentAudit, Appointment, Deal, FollowUpTask, Interaction, Lead,
        LeadLostDetail, LeadStageTransition, Offer, PropertyView, VisitFeedback,
    )
    async with get_new_session() as s:
        lead_ids = (
            await s.execute(
                select(Lead.id).where(Lead.listing_id.in_([l.id for l in listings]))
            )
        ).scalars().all()
        appt_ids = (
            await s.execute(
                select(Appointment.id).where(Appointment.lead_id.in_(lead_ids))
            )
        ).scalars().all() if lead_ids else []

        if appt_ids:
            await s.execute(delete(VisitFeedback).where(VisitFeedback.appointment_id.in_(appt_ids)))
        for model, col in (
            (Appointment, "lead_id"), (Interaction, "lead_id"), (Offer, "lead_id"),
            (Deal, "lead_id"), (FollowUpTask, "lead_id"), (AssignmentAudit, "lead_id"),
            (LeadLostDetail, "lead_id"), (LeadStageTransition, "lead_id"),
        ):
            if lead_ids:
                await s.execute(delete(model).where(getattr(model, col).in_(lead_ids)))
        if lead_ids:
            await s.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
        await s.execute(delete(PropertyView).where(
            PropertyView.listing_id.in_([l.id for l in listings])))
        await s.execute(delete(Listing).where(Listing.id.in_([l.id for l in listings])))

        from app.models import Property as P, Owner as O, Agency as A, Agent as G, Client as C
        await s.execute(delete(C).where(C.id.in_([c.id for c in clients])))
        await s.execute(delete(G).where(G.id.in_([a.id for pair in agents for a in pair])))
        prop_ids = [l.property_id for l in listings]
        owner_ids = (await s.execute(select(P.owner_id).where(P.id.in_(prop_ids)))).scalars().all()
        await s.execute(delete(P).where(P.id.in_(prop_ids)))
        await s.execute(delete(O).where(O.id.in_(owner_ids)))
        await s.execute(delete(A).where(A.id.in_([a.id for a in agencies])))
        await s.execute(delete(Person).where(Person.full_name.like(f"{tag}%")))
        await s.commit()


def get_new_session():
    from app.db import get_sessionmaker
    return get_sessionmaker()()


@pytest_asyncio.fixture
async def client_for():
    """Build an AsyncClient acting as a given agent.

    httpx + ASGITransport rather than TestClient: TestClient drives the app
    through a blocking portal on its OWN event loop, while these fixtures run on
    pytest-asyncio's. Sharing one asyncpg pool across two loops fails with
    "attached to a different loop". Going async keeps everything on one loop.

    Identity travels in a header rather than a captured closure, so several
    clients can coexist -- overwriting one global override would silently make
    every client act as whichever agent was registered last, which is exactly
    the bug the cross-tenant tests are supposed to catch.
    """
    from fastapi import Header, HTTPException
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.db import get_sessionmaker
    from app.deps import get_current_agent
    from app.main import app
    from app.models import Agent

    # NB: annotate with plain `str`, not `Request`. This module uses
    # `from __future__ import annotations`, so FastAPI resolves annotations from
    # MODULE globals -- a name imported inside this fixture is invisible to it,
    # and the parameter silently degrades into a required query param (422
    # "field required: query.request"). `str` is a builtin, so it always resolves.
    async def _agent_from_header(x_test_agent_id: str = Header(default="")):
        raw = x_test_agent_id
        if not raw:
            raise HTTPException(401, "test client sent no agent header")
        async with get_sessionmaker()() as s:
            agent = (
                await s.execute(
                    select(Agent).options(joinedload(Agent.person))
                    .where(Agent.id == uuid.UUID(raw))
                )
            ).scalar_one_or_none()
        if agent is None:
            raise HTTPException(403, "unknown test agent")
        return agent

    app.dependency_overrides[get_current_agent] = _agent_from_header
    opened = []

    def _make(agent):
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers={"X-Test-Agent-Id": str(agent.id)}, timeout=30.0)
        opened.append(c)
        return c

    yield _make

    for c in opened:
        await c.aclose()
    app.dependency_overrides.clear()

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


_DB_FIXTURES = {"session", "world", "client_for"}


def pytest_collection_modifyitems(config, items):
    """Skip only the tests that actually touch the database.

    test_generator.py builds tuples in memory and must run without a
    connection, so gating the whole suite on DATABASE_URL was too blunt.
    """
    if not _missing:
        return
    skip = pytest.mark.skip(
        reason=f"needs a live database; set {', '.join(_missing)} in .env"
    )
    for item in items:
        if _DB_FIXTURES.intersection(getattr(item, "fixturenames", ())):
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

    # Build layer by layer with one flush per FK level instead of one per row.
    agencies = [Agency(name=f"{tag} agency {i}") for i in range(2)]
    clients_p = [person(f"client {i}") for i in range(2)]
    owners_p = [person(f"owner {i}") for i in range(2)]
    agents_p = [[person(f"agent {a}.{g}") for g in range(2)] for a in range(2)]
    session.add_all(agencies + clients_p + owners_p + [p for pair in agents_p for p in pair])
    await session.flush()

    agents = [
        [
            Agent(
                person_id=agents_p[a][g].id, agency_id=agencies[a].id,
                role="TEAM_ADMIN" if g == 0 else "AGENT",
                auth_user_id=uuid.uuid4(),
            )
            for g in range(2)
        ]
        for a in range(2)
    ]
    owners = [Owner(person_id=owners_p[i].id) for i in range(2)]
    clients = [Client(person_id=clients_p[i].id) for i in range(2)]
    session.add_all([a for pair in agents for a in pair] + owners + clients)
    await session.flush()

    props = [
        Property(
            owner_id=owners[i].id, property_type="HOUSE", city=f"{tag} City",
            neighborhood="N", address="1 Test St", area_m2=100, bedrooms=3, bathrooms=2,
        )
        for i in range(2)
    ]
    session.add_all(props)
    await session.flush()

    listings = [
        Listing(
            property_id=props[i].id, agent_id=agents[i][0].id, operation_type="SALE",
            asking_price=100000, min_acceptable_price=90000,
        )
        for i in range(2)
    ]
    session.add_all(listings)
    await session.commit()

    # Snapshot plain UUIDs now. A test that calls session.rollback() expires
    # every ORM object attached to the shared session, so reading `.id` in
    # teardown would trigger a lazy refresh from the wrong context
    # (MissingGreenlet). Teardown uses these, never the objects.
    agency_ids = [a.id for a in agencies]
    client_ids = [c.id for c in clients]
    listing_ids = [l.id for l in listings]
    property_ids = [p.id for p in props]
    owner_ids = [o.id for o in owners]

    made.update(agencies=agencies, agents=agents, listings=listings, clients=clients)
    yield type("World", (), made)

    # ---- teardown ----
    #
    # Everything this fixture touches hangs off its two agencies, so we delete by
    # agency in strict reverse-FK order. Each statement runs in its own tiny
    # transaction (a fresh session per step): a Postgres error aborts only its
    # own transaction, so one failure can't strand the rest, and the final tag
    # sweep of pii.person still runs. That is the crash-safety the pre-003
    # teardown lacked -- the thing scripts/purge_test_rows.sql exists to mop up.
    from sqlalchemy import delete, select
    from app.models import (
        AssignmentAudit, Appointment, Deal, DomainEvent, FollowUpTask, Interaction,
        Lead, LeadLostDetail, LeadStageTransition, Offer, PropertyView, VisitFeedback,
    )
    from app.models import (
        AgentAvailability, AgentTimeOff, Property as P, Owner as O, Agency as A,
        Agent as G, Client as C,
    )

    async def _run(stmt):
        try:
            async with get_new_session() as s:
                await s.execute(stmt)
                await s.commit()
        except Exception as exc:  # noqa: BLE001 — teardown must not mask test results
            print(f"world teardown: {exc.__class__.__name__} on {stmt}")

    agent_ids = select(G.id).where(G.agency_id.in_(agency_ids))
    lease_scope = select(Lead.id).where(Lead.agent_id.in_(agent_ids))
    appt_scope = select(Appointment.id).where(Appointment.lead_id.in_(lease_scope))

    await _run(delete(VisitFeedback).where(VisitFeedback.appointment_id.in_(appt_scope)))
    for model in (Appointment, Interaction, Offer, Deal, FollowUpTask,
                  AssignmentAudit, LeadLostDetail, LeadStageTransition):
        await _run(delete(model).where(model.lead_id.in_(lease_scope)))
    await _run(delete(Lead).where(Lead.agent_id.in_(agent_ids)))
    await _run(delete(DomainEvent).where(DomainEvent.agency_id.in_(agency_ids)))
    await _run(delete(PropertyView).where(PropertyView.listing_id.in_(listing_ids)))
    await _run(delete(Listing).where(Listing.id.in_(listing_ids)))
    await _run(delete(AgentAvailability).where(AgentAvailability.agent_id.in_(agent_ids)))
    await _run(delete(AgentTimeOff).where(AgentTimeOff.agent_id.in_(agent_ids)))
    await _run(delete(C).where(C.id.in_(client_ids)))
    await _run(delete(G).where(G.agency_id.in_(agency_ids)))
    await _run(delete(P).where(P.id.in_(property_ids)))
    await _run(delete(O).where(O.id.in_(owner_ids)))
    await _run(delete(A).where(A.id.in_(agency_ids)))
    await _run(delete(Person).where(Person.full_name.like(f"{tag}%")))


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

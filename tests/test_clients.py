"""POST /clients (008): register a client, deduplicated by Telegram account only.

Like test_dedup.py, the concurrency test is the one that matters — a
Python-side "look it up first" would pass the sequential tests and still
create two clients for one Telegram user in production.
"""
import asyncio
import random

from sqlalchemy import func, select

from tests.conftest import get_new_session
from tests.test_service_account import _set_account


def _tg() -> int:
    # Real Telegram ids are well above 2^31; a random one keeps tests independent.
    return random.randint(10**10, 10**12)


def _body(world, name="walk-in", **extra):
    # The world tag prefix is what teardown and scripts/purge_test_rows.sql sweep.
    return {"full_name": f"{world.tag} {name}", **extra}


async def test_without_telegram_id_every_post_is_a_new_client(world, client_for):
    c = client_for(world.agents[0][1])
    body = _body(world, phone="3001234567", email="walkin@example.com")

    first = await c.post("/clients", json=body)
    second = await c.post("/clients", json=body)
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]      # deliberate: no fuzzy match


async def test_same_telegram_id_returns_the_existing_client(world, client_for):
    c = client_for(world.bots[0])
    tg = _tg()

    first = await c.post("/clients", json=_body(world, "Ana", telegram_user_id=tg))
    assert first.status_code == 201, first.text

    again = await c.post("/clients", json=_body(world, "Ana Renamed", telegram_user_id=tg,
                                                phone="3009999999"))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]

    from app.models import Client, Person
    async with get_new_session() as s:
        name, phone = (await s.execute(
            select(Person.full_name, Person.phone)
            .join(Client, Client.person_id == Person.id)
            .where(Client.id == first.json()["id"])
        )).one()
    assert name == f"{world.tag} Ana" and phone is None    # first write wins


async def test_concurrent_first_messages_produce_one_client(world, client_for):
    c = client_for(world.bots[0])
    body = _body(world, "racer", telegram_user_id=_tg())

    results = await asyncio.gather(*[c.post("/clients", json=body) for _ in range(6)])
    codes = sorted(r.status_code for r in results)
    ids = {r.json()["id"] for r in results}

    assert codes.count(201) == 1 and codes.count(200) == 5, codes
    assert len(ids) == 1

    from app.models import Person
    async with get_new_session() as s:
        n = await s.scalar(select(func.count()).select_from(Person)
                           .where(Person.telegram_user_id == body["telegram_user_id"]))
    assert n == 1


async def test_response_never_echoes_contact_details(world, client_for):
    c = client_for(world.agents[1][1])
    r = await c.post("/clients", json=_body(world, phone="3001112233",
                                            email="x@example.com", telegram_user_id=_tg()))
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"id", "created_at"}


async def test_validation(world, client_for):
    c = client_for(world.agents[0][1])
    assert (await c.post("/clients", json={"full_name": ""})).status_code == 422
    assert (await c.post("/clients", json=_body(world, telegram_user_id=0))).status_code == 422


async def test_bot_needs_the_clients_create_scope(world, client_for):
    bot = client_for(world.bots[0])
    assert (await bot.post("/clients", json=_body(world, "bot-ok"))).status_code == 201

    await _set_account(world, scopes=[s for s in world.service_account.scopes
                                      if s != "clients:create"])
    r = await bot.post("/clients", json=_body(world, "bot-denied"))
    assert r.status_code == 403 and "clients:create" in r.json()["detail"]


async def test_registered_client_can_open_a_lead(world, client_for):
    """The point of the endpoint: a first-time Telegram user gets a lead."""
    bot = client_for(world.bots[0])
    r = await bot.post("/clients", json=_body(world, "new lead", telegram_user_id=_tg()))
    client_id = r.json()["id"]

    r = await bot.post("/leads", json={
        "client_id": client_id,
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
        "message": "Hola, ¿sigue disponible?",
    })
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == client_id
    assert r.json()["agent_id"] == str(world.agents[0][0].id)   # listing's agent owns it

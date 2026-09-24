"""HU-06: the board's cards and filters. Agency isolation is in test_tenancy.py."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings


async def _lead(c, world, client_idx=0):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _card(rows, lead_id):
    return next(row for row in rows if row["id"] == lead_id)


def _today():
    return datetime.now(ZoneInfo(get_settings().app_timezone)).date()


async def test_card_carries_client_and_property(world, client_for):
    """AC1: name, property of interest, last interaction."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)

    card = _card((await c.get("/leads")).json(), lead)
    assert card["client_name"] == f"{world.tag} client 0"
    assert card["listing_address"] == "1 Test St"
    assert card["operation_type"] == "SALE"
    assert float(card["asking_price"]) == 100000
    assert card["last_interaction"] is None


async def test_last_interaction_is_the_newest_entry(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    for direction, body in (("INBOUND", "Is it still available?"), ("OUTBOUND", "x" * 300)):
        r = await c.post(f"/leads/{lead}/interactions", json={
            "direction": direction, "channel": "TELEGRAM", "type": "MESSAGE", "body": body,
        })
        assert r.status_code == 201, r.text

    last = _card((await c.get("/leads")).json(), lead)["last_interaction"]
    assert last["direction"] == "OUTBOUND" and last["type"] == "MESSAGE"
    assert last["body"] == "x" * 140, "the card carries a preview, not the whole message"


async def test_property_filter(world, client_for):
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)

    mine = await c.get("/leads", params={"property_id": str(world.listings[0].property_id)})
    assert [row["id"] for row in mine.json()] == [lead]

    other = await c.get("/leads", params={"property_id": str(world.listings[1].property_id)})
    assert other.json() == []


async def test_created_date_range_is_inclusive_and_in_agency_timezone(world, client_for):
    """AC3: `created_to` covers the whole of that day."""
    c = client_for(world.agents[0][0])
    lead = await _lead(c, world)
    today, day = _today(), timedelta(days=1)

    async def ids(**params):
        r = await c.get("/leads", params={k: v.isoformat() for k, v in params.items()})
        assert r.status_code == 200, r.text
        return {row["id"] for row in r.json()}

    assert lead in await ids(created_from=today, created_to=today)
    assert lead in await ids(created_from=today - day)
    assert lead not in await ids(created_from=today + day)
    assert lead not in await ids(created_to=today - day)


async def test_created_range_backwards_is_rejected(world, client_for):
    c = client_for(world.agents[0][0])
    today = _today()
    r = await c.get("/leads", params={
        "created_from": today.isoformat(), "created_to": (today - timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 422, r.text

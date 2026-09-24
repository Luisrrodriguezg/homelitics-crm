"""Analytics endpoints that tests can pin exactly: the world fixture's
agencies are fresh, so their analytics rows are only what the test wrote."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings


async def _lead(c, world, client_idx):
    r = await c.post("/leads", json={
        "client_id": str(world.clients[client_idx].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_lost_reasons_feed_analytics(world, client_for):
    """HU-09 AC3, scoped to the caller's agency."""
    c = client_for(world.agents[0][0])
    for idx, reason in ((0, "PRICE"), (1, "NO_RESPONSE")):
        lead = await _lead(c, world, idx)
        r = await c.post(f"/leads/{lead}/transitions",
                         json={"to_stage": "LOST", "lost_reason": reason})
        assert r.status_code == 201, r.text

    rows = (await c.get("/analytics/lost-reasons")).json()
    assert sorted((row["reason"], row["leads"], row["pct"]) for row in rows) == [
        ("NO_RESPONSE", 1, 50.0), ("PRICE", 1, 50.0),
    ]

    intruder = client_for(world.agents[1][0])
    assert (await intruder.get("/analytics/lost-reasons")).json() == []


async def _walk(c, lead, *stages):
    for stage in stages:
        r = await c.post(f"/leads/{lead}/transitions", json={"to_stage": stage})
        assert r.status_code == 201, r.text


async def _funnel_world(c, world):
    """Two leads: one walked to WON, one lost straight away."""
    won, lost = await _lead(c, world, 0), await _lead(c, world, 1)
    await _walk(c, won, "VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON")
    await _walk(c, lost, "VISIT_SCHEDULED")
    r = await c.post(f"/leads/{lost}/transitions",
                     json={"to_stage": "LOST", "lost_reason": "PRICE"})
    assert r.status_code == 201, r.text


def _by_stage(body):
    return {row["stage"]: row for row in body["stages"]}


async def test_funnel_counts_and_conversion(world, client_for):
    """HU-17 AC1, and the unfiltered funnel agrees with the North Star stage table."""
    c = client_for(world.agents[0][0])
    await _funnel_world(c, world)

    body = (await c.get("/analytics/funnel")).json()
    got = {s: (r["leads_reached"], r["pct_from_prev"], r["pct_of_first"])
           for s, r in _by_stage(body).items()}
    assert got == {
        "INTERESTED": (2, None, 100.0),
        "VISIT_SCHEDULED": (2, 100.0, 100.0),
        "VISITED": (1, 50.0, 50.0),
        "NEGOTIATING": (1, 100.0, 50.0),
        "WON": (1, 100.0, 50.0),
    }
    assert body["lost"] == 1 and body["filters"] == {}

    north = (await c.get("/analytics/north-star")).json()["stage_conversion"]
    assert {r["stage"]: r["leads_reached"] for r in north if r["stage"] != "LOST"} == {
        s: r["leads_reached"] for s, r in _by_stage(body).items()
    }


async def test_funnel_filters(world, client_for):
    """HU-17 AC2: agent, property, sale/rent, and the creation window."""
    c = client_for(world.agents[0][0])
    await _funnel_world(c, world)
    today = datetime.now(ZoneInfo(get_settings().app_timezone)).date()

    async def interested(**params):
        r = await c.get("/analytics/funnel", params={k: str(v) for k, v in params.items()})
        assert r.status_code == 200, r.text
        return _by_stage(r.json())["INTERESTED"]["leads_reached"]

    assert await interested(agent_id=world.agents[0][0].id) == 2
    assert await interested(agent_id=world.agents[0][1].id) == 0
    assert await interested(property_id=world.listings[0].property_id) == 2
    assert await interested(property_id=world.listings[1].property_id) == 0
    assert await interested(operation_type="SALE") == 2
    assert await interested(operation_type="RENT") == 0
    assert await interested(created_from=today, created_to=today) == 2
    assert await interested(created_from=today + timedelta(days=1)) == 0
    assert await interested(created_to=today - timedelta(days=1)) == 0

    r = await c.get("/analytics/funnel", params={
        "created_from": str(today), "created_to": str(today - timedelta(days=1)),
    })
    assert r.status_code == 422, r.text


async def test_funnel_csv_export(world, client_for):
    """HU-17 AC3 (CSV; PDF is the frontend's)."""
    c = client_for(world.agents[0][0])
    await _funnel_world(c, world)

    r = await c.get("/analytics/funnel", params={"format": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "stage,leads_reached,pct_from_prev,pct_of_first"
    assert lines[1] == "INTERESTED,2,,100.0" and lines[-1] == "LOST,1,,50.0"
    assert len(lines) == 7


async def test_funnel_is_admin_only_and_tenant_scoped(world, client_for):
    c = client_for(world.agents[0][0])
    await _funnel_world(c, world)

    plain_agent = client_for(world.agents[0][1])
    assert (await plain_agent.get("/analytics/funnel")).status_code == 403

    intruder = client_for(world.agents[1][0])
    body = (await intruder.get("/analytics/funnel")).json()
    assert _by_stage(body)["INTERESTED"]["leads_reached"] == 0 and body["lost"] == 0

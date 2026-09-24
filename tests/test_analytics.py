"""Analytics endpoints that tests can pin exactly: the world fixture's
agencies are fresh, so their analytics rows are only what the test wrote."""


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

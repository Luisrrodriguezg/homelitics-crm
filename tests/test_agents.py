"""GET /agents: the reassignment picker. Agency isolation is the point."""
import uuid


async def test_lists_only_own_agency(world, client_for):
    c = client_for(world.agents[0][0])
    r = await c.get("/agents")
    assert r.status_code == 200, r.text
    ids = {a["id"] for a in r.json()}
    assert str(world.agents[0][1].id) in ids
    assert not ids & {str(a.id) for a in world.agents[1]}, "another agency leaked"
    assert {a["agency_id"] for a in r.json()} == {str(world.agents[0][0].agency_id)}


async def test_no_contact_details(world, client_for):
    rows = (await client_for(world.agents[0][0]).get("/agents")).json()
    assert all(set(a) == {"id", "agency_id", "role", "active", "full_name"} for a in rows)


async def test_agency_filter_accepts_own_and_rejects_other(world, client_for):
    c = client_for(world.agents[0][0])
    own = await c.get("/agents", params={"agency_id": str(world.agents[0][0].agency_id)})
    assert own.status_code == 200 and own.json()
    other = await c.get("/agents", params={"agency_id": str(world.agents[1][0].agency_id)})
    assert other.status_code == 404
    assert (await c.get("/agents", params={"agency_id": str(uuid.uuid4())})).status_code == 404


async def test_role_filter(world, client_for):
    c = client_for(world.agents[0][0])
    rows = (await c.get("/agents", params={"role": "TEAM_ADMIN"})).json()
    assert rows and {a["role"] for a in rows} == {"TEAM_ADMIN"}


async def test_listed_id_is_a_valid_reassign_target(world, client_for):
    admin = client_for(world.agents[0][0])
    target = next(a for a in (await admin.get("/agents")).json()
                  if a["id"] == str(world.agents[0][1].id))
    r = await admin.post("/leads", json={
        "client_id": str(world.clients[0].id),
        "listing_id": str(world.listings[0].id),
        "source_channel": "TELEGRAM",
    })
    assert r.status_code in (200, 201), r.text
    moved = await admin.post(f"/leads/{r.json()['id']}/reassign",
                             json={"to_agent_id": target["id"]})
    assert moved.status_code == 200, moved.text

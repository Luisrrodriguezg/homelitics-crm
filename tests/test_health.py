"""/health must fail when the database lacks a column the models map.

The code for migration 009 merged before the migration was applied. /health ran
only `select 1`, so Render's health check passed and every authenticated route
500'd until 009 went on. These pin the check that would have failed that deploy.

/health is open, so no client_for: a plain AsyncClient on the test's own loop.
"""
import pytest

from tests.conftest import _missing

pytestmark = pytest.mark.skipif(
    bool(_missing), reason=f"needs a live database; set {', '.join(_missing)} in .env"
)


async def _get_health():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/health")


async def test_health_is_ok_when_every_mapped_column_exists():
    r = await _get_health()
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "database": "ok", "schema": "ok"}


async def test_health_fails_when_a_mapped_column_is_missing(monkeypatch):
    from app.routers import health

    monkeypatch.setattr(
        health, "MAPPED_COLUMNS", health.MAPPED_COLUMNS | {("core", "agent", "nope")}
    )
    r = await _get_health()
    assert r.status_code == 503
    assert "core.agent.nope" in r.json()["detail"]

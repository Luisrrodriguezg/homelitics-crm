"""Every write route must declare a scope (or be TEAM_ADMIN-only).

Service-account authorization is deny-by-default *only if* every non-GET route
carries `Depends(require_scope(...))`. A new write route that forgets it would
be silently open to the bot — this test is what catches that. No database
needed; it only walks the router table.
"""
import os

import pytest
from fastapi.routing import APIRoute

if not os.environ.get("DATABASE_URL"):
    # app.main builds Settings at import time; without env it cannot load.
    pytest.skip("needs DATABASE_URL to import the app", allow_module_level=True)

READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _write_routes():
    from app.main import app
    return [r for r in app.routes
            if isinstance(r, APIRoute) and not r.methods <= READ_METHODS]


def test_every_write_route_is_scoped_or_admin_only():
    from app.deps import get_team_admin

    unguarded = []
    for route in _write_routes():
        scoped = any(getattr(d.dependency, "scope", None) for d in route.dependencies)
        admin_only = any(dep.call is get_team_admin for dep in route.dependant.dependencies)
        if not (scoped or admin_only):
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert not unguarded, f"write routes open to service accounts: {unguarded}"


def test_there_are_write_routes_to_check():
    """Guard against the walk silently matching nothing (e.g. a router renamed)."""
    assert len(_write_routes()) >= 10

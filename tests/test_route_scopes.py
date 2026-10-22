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

# Write routes with no principal at all, so there is nothing to scope. Each one
# must authenticate by something else; adding to this set needs that reason.
PUBLIC_WRITE_ROUTES = {
    # The invite token in the body is the credential (012, DECISIONS §21).
    "POST /invites/inspect",
    "POST /invites/accept",
}


def _write_routes():
    from app.main import app
    return [r for r in app.routes
            if isinstance(r, APIRoute) and not r.methods <= READ_METHODS]


def test_every_write_route_is_scoped_or_admin_only():
    from app.deps import get_team_admin

    unguarded = []
    for route in _write_routes():
        if any(f"{m} {route.path}" in PUBLIC_WRITE_ROUTES for m in route.methods):
            continue
        scoped = any(getattr(d.dependency, "scope", None) for d in route.dependencies)
        admin_only = any(dep.call is get_team_admin for dep in route.dependant.dependencies)
        if not (scoped or admin_only):
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert not unguarded, f"write routes open to service accounts: {unguarded}"


def test_public_write_routes_take_no_agent():
    """The allow-list must not hide a route that does act as an agent."""
    from app.deps import get_current_agent

    def _needs_agent(dependant):
        return any(d.call is get_current_agent or _needs_agent(d) for d in dependant.dependencies)

    public = [r for r in _write_routes()
              if any(f"{m} {r.path}" in PUBLIC_WRITE_ROUTES for m in r.methods)]
    assert len(public) == len(PUBLIC_WRITE_ROUTES), "an allow-listed route no longer exists"
    assert not [r.path for r in public if _needs_agent(r.dependant)]


def test_there_are_write_routes_to_check():
    """Guard against the walk silently matching nothing (e.g. a router renamed)."""
    assert len(_write_routes()) >= 10

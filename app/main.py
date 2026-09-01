"""Homelitics API — real-estate lead management.

Authorization note: this service filters by agency_id in the service layer
rather than relying on Postgres RLS. That is a deliberate decision forced by the
transaction pooler; see docs/DECISIONS.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import dispose_engine
from app.jobs import build_scheduler
from app.routers import (
    analytics, appointments, availability, health, leads, listings,
)

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("homelitics")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = build_scheduler()
    if scheduler:
        scheduler.start()
        log.info(
            "inactivity sweep every %d min (threshold %dh)",
            settings.scheduler_interval_minutes, settings.inactivity_hours,
        )
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        await dispose_engine()


DESCRIPTION = """
Lead-management API for real estate agencies.

The product is the **funnel**, not the catalogue: a client contacts an agent
about a listing, the agent responds, a visit happens, it closes or it dies.

### Authentication
Every route except `/health` and the docs needs a Supabase access token:
`Authorization: Bearer <token>`. The token's `sub` claim is resolved to a
`core.agent` row, and that row's `agency_id` scopes every query.

* **401** — missing, expired or invalid token
* **403** — valid token, but no agent is bound to that user (run `scripts/bind_agents.py`)

### Two rules worth knowing
* **Transitions are truth.** `POST /leads/{id}/transitions` appends to the log; a
  database trigger updates the `current_stage` cache. Never the other way round.
* **Leads are deduplicated by the database.** `(client_id, listing_id)` is UNIQUE, so
  `POST /leads` returns **201** for a new thread and **200** for one that already exists.
"""

app = FastAPI(
    title="Homelitics API",
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness. Open — no token required."},
        {"name": "identity", "description": "Who the current token belongs to."},
        {"name": "leads", "description": "The funnel: board, transitions, timeline, tasks."},
        {"name": "appointments", "description": "Visit requests, confirmation, feedback."},
        {"name": "availability", "description": "Agent weekly availability, time off, free slots."},
        {"name": "listings", "description": "Catalogue and view events."},
        {"name": "analytics", "description": "Reads the analytics schema only, never core."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Service-layer ValueErrors are client mistakes, not 500s."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
    )


app.include_router(health.router)
app.include_router(leads.router)
app.include_router(appointments.lead_router)
app.include_router(appointments.router)
app.include_router(availability.router)
app.include_router(listings.router)
app.include_router(analytics.router)

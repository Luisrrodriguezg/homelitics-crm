"""Request dependencies: resolve a bearer token to the acting agent.

This is where authorization starts. Every service call takes an agency_id that
originates here, from the database row the token maps to — never from anything
the client sent. See docs/DECISIONS.md for why filtering lives in the service
layer rather than in RLS.

Two kinds of principal resolve to the same `Agent` object (docs/DECISIONS.md §17):

* a human — token `sub` matches `core.agent.auth_user_id`;
* a service account (an AI agent) — token `sub` matches
  `core.service_account.auth_user_id`, and `X-Agency-Id` picks which of that
  account's per-agency AI_AGENT rows to act as. The header only chooses among
  rows the account already owns, so `agency_id` still comes from the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import (
    APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.auth import InvalidToken, decode_token, unauthorized
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Agent, Interaction, LeadStageTransition, ServiceAccount

# auto_error=False so a missing header produces our 401 with WWW-Authenticate
# rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")

# Only meaningful when DEV_AUTH_BYPASS is on (compose `local` profile). Declared
# as a security scheme purely so Swagger renders an "Authorize" box for it —
# paste a core.agent UUID there and every "Try it out" carries the header.
#
# Each header scheme needs its own scheme_name: FastAPI otherwise keys them all
# as "APIKeyHeader" in the OpenAPI spec and the last one silently replaces the
# others (app/main.py's dev rewrite looks these names up).
dev_agent_scheme = APIKeyHeader(
    name="X-Dev-Agent-Id", auto_error=False, scheme_name="DevAgentId",
    description="DEV_AUTH_BYPASS only: a core.agent UUID to act as.",
)

# Service accounts only: which agency's AI_AGENT row to act as. Ignored for
# human tokens.
agency_scheme = APIKeyHeader(
    name="X-Agency-Id", auto_error=False, scheme_name="AgencyId",
    description="Service accounts only: the core.agency UUID to act in.",
)


def _agent_query():
    return select(Agent).options(joinedload(Agent.person), joinedload(Agent.service_account))


async def _resolve_service_account(
    session: AsyncSession, sub: str, x_agency_id: str | None
) -> Agent:
    """A token that matched no human agent: try the service accounts.

    403 for an unknown or inactive account (same shape as an unbound human, so
    a caller learns nothing about which kind of principal it failed as), 400 for
    a missing/invalid X-Agency-Id, 403 for an agency the account has no row in.
    """
    account = (
        await session.execute(select(ServiceAccount).where(ServiceAccount.auth_user_id == sub))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Authenticated, but this user is not linked to an agent. "
                "Run scripts/bind_agents.py to bind the Supabase user to a core.agent row."
            ),
        )
    if not account.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Service account is deactivated")

    if not x_agency_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Service accounts must send X-Agency-Id")
    try:
        agency_id = uuid.UUID(x_agency_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Agency-Id is not a UUID")

    agent = (
        await session.execute(
            _agent_query().where(
                Agent.service_account_id == account.id, Agent.agency_id == agency_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This service account has no AI_AGENT row in that agency. "
            "Re-run scripts/provision_ai_agent.sql.",
        )
    return agent


async def get_current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_dev_agent_id: Annotated[str | None, Depends(dev_agent_scheme)] = None,
    x_agency_id: Annotated[str | None, Depends(agency_scheme)] = None,
) -> Agent:
    """401 for a missing/invalid token, 403 for a valid token with no agent row."""
    if settings.dev_auth_bypass:
        # Local profile only (config refuses this against a non-local DB). Identity
        # comes straight from a header — no signature checking.
        if not x_dev_agent_id:
            raise unauthorized("DEV_AUTH_BYPASS is on; send X-Dev-Agent-Id")
        try:
            agent_id = uuid.UUID(x_dev_agent_id)
        except ValueError:
            raise unauthorized("X-Dev-Agent-Id is not a UUID")
        agent = (
            await session.execute(_agent_query().where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown dev agent id")
        return agent

    if credentials is None or not credentials.credentials:
        raise unauthorized()

    try:
        # PyJWT's JWKS client does blocking network I/O on a cache miss.
        claims = await run_in_threadpool(decode_token, credentials.credentials, settings)
    except InvalidToken:
        # Deliberately vague: never tell a caller *why* their token failed.
        raise unauthorized("Invalid or expired token")

    agent = (
        await session.execute(_agent_query().where(Agent.auth_user_id == claims.sub))
    ).scalar_one_or_none()

    if agent is None:
        agent = await _resolve_service_account(session, claims.sub, x_agency_id)

    if not agent.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent is deactivated")

    return agent


async def get_team_admin(
    agent: Annotated[Agent, Depends(get_current_agent)],
) -> Agent:
    """For routes only a TEAM_ADMIN may call (reassignment)."""
    if agent.role != "TEAM_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the TEAM_ADMIN role",
        )
    return agent


# ------------------------------------------------------------------ scopes
#
# Humans are unaffected by all of this: a scope check is a no-op for them.
# For a service account each write route names the scope it needs; the
# account's `scopes` array must contain it, and the rolling-hour write budget
# must not be spent. tests/test_route_scopes.py asserts every non-GET route
# declares one, so a new write route cannot silently be open to bots.

def has_scope(agent: Agent, scope: str) -> bool:
    if not agent.is_bot:
        return True
    account = agent.service_account
    return account is not None and scope in account.scopes


async def _enforce_write_budget(session: AsyncSession, agent: Agent) -> None:
    """429 once this account's bot rows have written `hourly_write_limit`
    transitions + interactions in the last hour. Counted across all of the
    account's agencies, which is the point: one runaway loop, one brake.

    Two near-simultaneous requests can both pass and land one over the limit.
    Acceptable — this stops a loop, it is not billing.
    """
    account = agent.service_account
    if account is None:
        return
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    bot_ids = select(Agent.id).where(Agent.service_account_id == account.id)

    transitions = await session.scalar(
        select(func.count()).select_from(LeadStageTransition).where(
            LeadStageTransition.changed_by.in_(bot_ids),
            LeadStageTransition.changed_at >= since,
        )
    )
    interactions = await session.scalar(
        select(func.count()).select_from(Interaction).where(
            Interaction.created_by.in_(bot_ids), Interaction.occurred_at >= since
        )
    )
    used = (transitions or 0) + (interactions or 0)

    if used >= account.hourly_write_limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Service account write budget spent: {used}/{account.hourly_write_limit} "
            "writes in the last hour",
            headers={"Retry-After": "3600"},
        )


def require_scope(scope: str):
    """Route dependency: `dependencies=[Depends(require_scope("leads:create"))]`."""

    async def _check(
        agent: Annotated[Agent, Depends(get_current_agent)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> None:
        if not agent.is_bot:
            return
        if not has_scope(agent, scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Service account lacks the {scope!r} scope",
            )
        await _enforce_write_budget(session, agent)

    _check.scope = scope  # read by tests/test_route_scopes.py
    return _check


CurrentAgent = Annotated[Agent, Depends(get_current_agent)]
TeamAdmin = Annotated[Agent, Depends(get_team_admin)]
DbSession = Annotated[AsyncSession, Depends(get_session)]

"""Request dependencies: resolve a bearer token to the acting agent.

This is where authorization starts. Every service call takes an agency_id that
originates here, from the database row the token maps to — never from anything
the client sent. See docs/DECISIONS.md for why filtering lives in the service
layer rather than in RLS.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import (
    APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.auth import InvalidToken, decode_token, unauthorized
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Agent

# auto_error=False so a missing header produces our 401 with WWW-Authenticate
# rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")

# Only meaningful when DEV_AUTH_BYPASS is on (compose `local` profile). Declared
# as a security scheme purely so Swagger renders an "Authorize" box for it —
# paste a core.agent UUID there and every "Try it out" carries the header.
dev_agent_scheme = APIKeyHeader(
    name="X-Dev-Agent-Id", auto_error=False,
    description="DEV_AUTH_BYPASS only: a core.agent UUID to act as.",
)


async def get_current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_dev_agent_id: Annotated[str | None, Depends(dev_agent_scheme)] = None,
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
            await session.execute(
                select(Agent).options(joinedload(Agent.person)).where(Agent.id == agent_id)
            )
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
        await session.execute(
            select(Agent)
            .options(joinedload(Agent.person))
            .where(Agent.auth_user_id == claims.sub)
        )
    ).scalar_one_or_none()

    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Authenticated, but this user is not linked to an agent. "
                "Run scripts/bind_agents.py to bind the Supabase user to a core.agent row."
            ),
        )
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


CurrentAgent = Annotated[Agent, Depends(get_current_agent)]
TeamAdmin = Annotated[Agent, Depends(get_team_admin)]
DbSession = Annotated[AsyncSession, Depends(get_session)]

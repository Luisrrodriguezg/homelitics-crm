"""Open routes: liveness and identity."""
from fastapi import APIRouter, status
from sqlalchemy import text

from app.deps import CurrentAgent, DbSession
from app.schemas import AgentOut

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness and database connectivity",
    description="Open route. Returns 200 only if a round trip to Postgres succeeds, "
                "so it is safe to use as a container healthcheck.",
    responses={503: {"description": "Database unreachable"}},
)
async def health(session: DbSession):
    from fastapi import HTTPException
    try:
        await session.execute(text("select 1"))
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database unreachable: {exc}")
    return {"status": "ok", "database": "ok"}


@router.get(
    "/me",
    response_model=AgentOut,
    tags=["identity"],
    summary="The agent behind the current token",
    description="Resolves the Supabase JWT `sub` claim to a core.agent row. "
                "Use this first when debugging auth: 401 means the token is bad, "
                "403 means the token is fine but the user is not bound to an agent.",
    responses={
        401: {"description": "Missing, expired or invalid token"},
        403: {"description": "Valid token, but no agent is bound to this user"},
    },
)
async def me(agent: CurrentAgent):
    return AgentOut(
        id=agent.id,
        agency_id=agent.agency_id,
        role=agent.role,
        active=agent.active,
        full_name=agent.person.full_name if agent.person else None,
        email=agent.person.email if agent.person else None,
    )

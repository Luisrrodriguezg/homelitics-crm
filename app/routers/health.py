"""Open routes: liveness and identity."""
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from app.deps import CurrentAgent, DbSession
from app.models import Base
from app.schemas import AgentOut

router = APIRouter(tags=["health"])

# Every (schema, table, column) the models map. Code that is ahead of its
# migration selects columns the database lacks: 009's code merged before 009 was
# applied, and every authenticated route 500'd until it was, while a bare
# `select 1` here said "ok". Checking the columns makes Render's health check
# refuse such a deploy, so the previous build keeps serving.
MAPPED_COLUMNS = frozenset(
    (t.schema, t.name, c.name) for t in Base.metadata.tables.values() for c in t.columns
)
_SCHEMAS = sorted({schema for schema, _, _ in MAPPED_COLUMNS})


@router.get(
    "/health",
    summary="Liveness, database connectivity and schema",
    description="Open route. Returns 200 only if Postgres answers and has every column "
                "the models map, so it is safe to use as a container healthcheck: a "
                "deploy that lands before its migration fails it.",
    responses={503: {"description": "Database unreachable, or schema behind the code"}},
)
async def health(session: DbSession):
    try:
        rows = await session.execute(
            text("select table_schema, table_name, column_name "
                 "from information_schema.columns "
                 "where table_schema::text = any(:schemas)"),
            {"schemas": _SCHEMAS},
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database unreachable: {exc}")
    missing = sorted(".".join(col) for col in MAPPED_COLUMNS - {tuple(r) for r in rows})
    if missing:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"schema behind the code, apply the pending migration; missing: {missing}",
        )
    return {"status": "ok", "database": "ok", "schema": "ok"}


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

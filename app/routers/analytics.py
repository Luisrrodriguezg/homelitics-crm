"""Analytics endpoints. Every one reads the analytics schema only."""
import uuid

from fastapi import APIRouter, Query

from app.deps import CurrentAgent, DbSession
from app.schemas import (
    AgentResponseTimeOut, FunnelDailyOut, ListingPerformanceOut, NorthStarOut,
)
from app.services import analytics as svc

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/funnel-daily",
    response_model=list[FunnelDailyOut],
    summary="Daily stage transitions",
    description="Transition counts per day and target stage, scoped to the caller's agency.",
)
async def funnel_daily(
    agent: CurrentAgent,
    session: DbSession,
    days: int = Query(90, ge=1, le=730, description="Look-back window in days"),
):
    return await svc.funnel_daily(session, agency_id=agent.agency_id, days=days)


@router.get(
    "/agent-response-time",
    response_model=list[AgentResponseTimeOut],
    summary="First-response time per agent",
    description="Median and average hours from lead creation to the agent's first "
                "OUTBOUND interaction, plus how many leads were never answered. "
                "Slowest first. On seeded data the injected slow cohort shows here.",
)
async def agent_response_time(agent: CurrentAgent, session: DbSession):
    return await svc.agent_response_time(session, agency_id=agent.agency_id)


@router.get(
    "/listing-performance",
    response_model=list[ListingPerformanceOut],
    summary="Views, leads, visits and wins per listing",
    description="Ordered by views. On seeded data the overpriced cohort shows high "
                "views with a low win rate.",
)
async def listing_performance(
    agent: CurrentAgent,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return await svc.listing_performance(
        session, agency_id=agent.agency_id, limit=limit, offset=offset
    )


@router.get(
    "/north-star",
    response_model=NorthStarOut,
    summary="The five North Star metrics",
    description="Median time to first response, share of leads with a follow-up, "
                "lead-to-visit conversion, share lost within 48h, and stage-to-stage "
                "conversion across the funnel.",
)
async def north_star(agent: CurrentAgent, session: DbSession):
    return await svc.north_star(session, agency_id=agent.agency_id)

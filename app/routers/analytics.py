"""Analytics endpoints. Every one reads the analytics schema only."""
import csv
import io
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.deps import CurrentAgent, DbSession, TeamAdmin
from app.schemas import (
    AgentResponseTimeOut, FunnelDailyOut, FunnelOut, ListingPerformanceOut,
    LostReasonOut, Message, NorthStarOut, OperationType,
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
    "/lost-reasons",
    response_model=list[LostReasonOut],
    summary="Why leads were lost",
    description="Leads lost in the look-back window, grouped by the reason recorded "
                "when they moved to LOST, most common first. `pct` is the share of "
                "those lost leads, so the rows sum to 100.",
)
async def lost_reasons(
    agent: CurrentAgent,
    session: DbSession,
    days: int = Query(90, ge=1, le=730, description="Look-back window in days, on the loss date"),
):
    return await svc.lost_reasons(session, agency_id=agent.agency_id, days=days)


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


@router.get(
    "/funnel",
    response_model=FunnelOut,
    summary="Aggregated funnel, filterable and exportable",
    description="HU-17. Of the leads created in the window, how many ever reached "
                "each stage (INTERESTED → VISIT_SCHEDULED → VISITED → NEGOTIATING → "
                "WON), with the conversion from the previous stage and from the "
                "first, plus how many were lost. The biggest drop in `pct_from_prev` "
                "is where clients are lost. Filter by creation day (inclusive, "
                "agency timezone), agent, listing, property and sale/rent. "
                "`format=csv` downloads the same rows; PDF is the frontend's job. "
                "Team administrators only.",
    responses={
        200: {"content": {"text/csv": {}}, "description": "JSON, or CSV with format=csv"},
        403: {"model": Message, "description": "Not a TEAM_ADMIN"},
        422: {"model": Message, "description": "created_from is after created_to"},
    },
)
async def funnel(
    agent: TeamAdmin,
    session: DbSession,
    created_from: date | None = Query(None, description="Leads created on or after this day"),
    created_to: date | None = Query(None, description="Leads created on or before this day"),
    agent_id: uuid.UUID | None = Query(None, description="Filter by owning agent"),
    listing_id: uuid.UUID | None = Query(None, description="Filter by listing"),
    property_id: uuid.UUID | None = Query(None, description="Filter by property (SALE and RENT listings)"),
    operation_type: OperationType | None = Query(None, description="SALE or RENT"),
    format: Literal["json", "csv"] = Query("json"),
):
    if created_from and created_to and created_from > created_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "created_from is after created_to"
        )
    result = await svc.funnel(
        session, agency_id=agent.agency_id, created_from=created_from,
        created_to=created_to, agent_id=agent_id, listing_id=listing_id,
        property_id=property_id, operation_type=operation_type,
    )
    if format == "json":
        return result

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["stage", "leads_reached", "pct_from_prev", "pct_of_first"])
    for row in result["stages"]:
        writer.writerow([row["stage"], row["leads_reached"],
                         row["pct_from_prev"], row["pct_of_first"]])
    first = result["stages"][0]["leads_reached"]
    writer.writerow(["LOST", result["lost"], "",
                     round(100.0 * result["lost"] / first, 2) if first else ""])
    return Response(
        out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="funnel.csv"'},
    )

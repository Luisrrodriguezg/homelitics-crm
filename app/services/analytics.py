"""Analytics. Reads the analytics schema ONLY — never core.

That rule is the reason the analytics views exist, and the reason 002_fixes.sql
had to put agency_id on every one of them: without it these queries could not be
filtered per tenant and would have to reach into core, collapsing the boundary.

Raw SQL rather than ORM: these are plain views with no primary key, so mapping
them buys nothing.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def funnel_daily(
    session: AsyncSession, *, agency_id: uuid.UUID, days: int = 90
) -> list[dict]:
    rows = await session.execute(
        text("""
            select day, to_stage, transitions
            from analytics.funnel_daily
            where agency_id = :agency_id
              and day >= current_date - make_interval(0, 0, 0, :days)
            order by day, to_stage
        """),
        {"agency_id": agency_id, "days": days},
    )
    return [dict(r) for r in rows.mappings()]


async def agent_response_time(session: AsyncSession, *, agency_id: uuid.UUID) -> list[dict]:
    """Median/avg first response per agent, in hours.

    Joins pii.person only for the display name — the metric itself comes
    entirely from the view.
    """
    rows = await session.execute(
        text("""
            select art.agent_id,
                   p.full_name as agent_name,
                   art.leads,
                   extract(epoch from art.avg_first_response)    / 3600.0 as avg_first_response_hours,
                   extract(epoch from art.median_first_response) / 3600.0 as median_first_response_hours,
                   art.never_answered
            from analytics.agent_response_time art
            join core.agent a  on a.id = art.agent_id
            join pii.person p  on p.id = a.person_id
            where art.agency_id = :agency_id
            order by median_first_response_hours desc nulls last
        """),
        {"agency_id": agency_id},
    )
    return [dict(r) for r in rows.mappings()]


async def listing_performance(
    session: AsyncSession, *, agency_id: uuid.UUID, limit: int = 100, offset: int = 0
) -> list[dict]:
    rows = await session.execute(
        text("""
            select listing_id, operation_type, city, neighborhood, property_type,
                   asking_price, views, leads, visits, won
            from analytics.listing_performance
            where agency_id = :agency_id
            order by views desc
            limit :limit offset :offset
        """),
        {"agency_id": agency_id, "limit": limit, "offset": offset},
    )
    return [dict(r) for r in rows.mappings()]


async def north_star(session: AsyncSession, *, agency_id: uuid.UUID) -> dict:
    """The five target metrics from the backlog, in one payload."""
    summary = (
        await session.execute(
            text("""
                select count(*)                                                  as leads,
                       extract(epoch from percentile_cont(0.5) within group
                         (order by first_response_time)) / 3600.0                as median_first_response_hours,
                       100.0 * avg(case when has_follow_up   then 1 else 0 end)  as pct_with_follow_up,
                       100.0 * avg(case when reached_visit   then 1 else 0 end)  as lead_to_visit_conversion_pct,
                       100.0 * avg(case when lost_within_48h then 1 else 0 end)  as pct_lost_within_48h
                from analytics.lead_outcome
                where agency_id = :agency_id
            """),
            {"agency_id": agency_id},
        )
    ).mappings().one()

    stages = await session.execute(
        text("""
            select stage, sort_order, leads_reached, leads_prev_stage, pct_from_prev
            from analytics.stage_conversion
            where agency_id = :agency_id
            order by sort_order
        """),
        {"agency_id": agency_id},
    )

    def _round(v):
        return None if v is None else round(float(v), 2)

    return {
        "leads": summary["leads"],
        "median_first_response_hours": _round(summary["median_first_response_hours"]),
        "pct_with_follow_up": _round(summary["pct_with_follow_up"]) or 0.0,
        "lead_to_visit_conversion_pct": _round(summary["lead_to_visit_conversion_pct"]) or 0.0,
        "pct_lost_within_48h": _round(summary["pct_lost_within_48h"]) or 0.0,
        "stage_conversion": [
            {
                "stage": r["stage"],
                "sort_order": r["sort_order"],
                "leads_reached": r["leads_reached"],
                "leads_prev_stage": r["leads_prev_stage"],
                "pct_from_prev": _round(r["pct_from_prev"]),
            }
            for r in stages.mappings()
        ],
    }

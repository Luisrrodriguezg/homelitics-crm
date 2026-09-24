-- ============================================================
-- 010_lost_reason_analytics.sql — the lost reason reaches analytics
-- (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: HU-09 AC3 — "the reasons feed into the analytics module".
-- core.lead_lost_detail has been written since HU-09 was built, but
-- dashboards read analytics.* only (CLAUDE.md), and no view exposed it.
-- Rather than a sixth view, analytics.lead_outcome gains one column:
-- it is already one row per lead with lost_at, so the reason belongs
-- next to it. GET /analytics/lost-reasons groups on it.
-- ============================================================

-- ------------------------------------------------------------
-- analytics.lead_outcome: the 007 definition verbatim, plus
-- `lost_reason` APPENDED LAST — CREATE OR REPLACE VIEW only allows new
-- columns at the end. lead_lost_detail's primary key is lead_id, so the
-- left join adds at most one row per lead: every North Star metric that
-- reads this view is unchanged.
-- ------------------------------------------------------------
create or replace view analytics.lead_outcome as
select l.id            as lead_id,
       ag.agency_id,
       l.agent_id,
       l.listing_id,
       l.source_channel,
       l.created_at,
       l.current_stage,
       ls.is_terminal,
       fr.first_outbound,
       fr.first_outbound - l.created_at as first_response_time,
       (fr.first_outbound is not null)  as has_outbound,
       exists (select 1 from core.lead_stage_transition t
               where t.lead_id = l.id and t.to_stage = 'VISITED') as reached_visit,
       exists (select 1 from core.follow_up_task f
               where f.lead_id = l.id)                            as has_follow_up,
       lt.lost_at,
       (lt.lost_at is not null
        and lt.lost_at - l.created_at <= interval '48 hours')      as lost_within_48h,
       lr.code                                                     as lost_reason
from core.lead l
join core.agent ag      on ag.id = l.agent_id
join core.lead_stage ls on ls.code = l.current_stage
left join lateral (
  select min(i.occurred_at) as first_outbound
  from core.interaction i
  left join core.agent b on b.id = i.created_by
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
    and i.type in ('MESSAGE','CALL')
    and b.role is distinct from 'AI_AGENT'
) fr on true
left join lateral (
  select min(t.changed_at) as lost_at
  from core.lead_stage_transition t
  where t.lead_id = l.id and t.to_stage = 'LOST'
) lt on true
left join core.lead_lost_detail lld on lld.lead_id = l.id
left join core.lost_reason lr       on lr.id = lld.lost_reason_id;

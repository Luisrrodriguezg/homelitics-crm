-- ============================================================
-- 011_funnel_filters.sql — the funnel dashboard can be filtered
-- (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: HU-17 AC2 — filter the aggregated funnel by property,
-- sale/rent and agent. Dashboards read analytics.* only (CLAUDE.md),
-- and no view carried the property or the operation type, nor a
-- per-lead flag for every funnel stage. As in 010, analytics.lead_outcome
-- (already one row per lead, with agency, agent, listing and created_at)
-- gains columns instead of a sixth view appearing.
-- ============================================================

-- ------------------------------------------------------------
-- analytics.lead_outcome: the 010 definition verbatim, plus five columns
-- APPENDED LAST — CREATE OR REPLACE VIEW only allows new columns at the
-- end. lead.listing_id is NOT NULL, so the listing join keeps every row
-- and adds none; the transition aggregate is one row per lead. Every
-- North Star metric that reads this view is unchanged.
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
       lr.code                                                     as lost_reason,
       li.property_id,
       li.operation_type,
       coalesce(st.reached_visit_scheduled, false)                 as reached_visit_scheduled,
       coalesce(st.reached_negotiating, false)                     as reached_negotiating,
       coalesce(st.reached_won, false)                             as reached_won
from core.lead l
join core.agent ag      on ag.id = l.agent_id
join core.lead_stage ls on ls.code = l.current_stage
join core.listing li    on li.id = l.listing_id
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
left join lateral (
  select bool_or(t.to_stage = 'VISIT_SCHEDULED') as reached_visit_scheduled,
         bool_or(t.to_stage = 'NEGOTIATING')     as reached_negotiating,
         bool_or(t.to_stage = 'WON')             as reached_won
  from core.lead_stage_transition t
  where t.lead_id = l.id
) st on true
left join core.lead_lost_detail lld on lld.lead_id = l.id
left join core.lost_reason lr       on lr.id = lld.lost_reason_id;

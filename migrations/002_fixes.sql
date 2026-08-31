-- ============================================================
-- 002_fixes.sql — additive fixes on top of 001_schema.sql
--
-- Nothing here drops or rewrites data. Safe to run against a
-- populated database; safe to re-run (idempotent throughout).
--
-- Why each change exists is documented in docs/DECISIONS.md.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Supabase Auth link
--    The JWT `sub` claim is a Supabase auth.users UUID. The API
--    resolves it to an agent row through this column. Nullable:
--    seeded agents have no auth user until bind_agents.py runs,
--    and Postgres allows many NULLs under a unique index.
-- ------------------------------------------------------------
alter table core.agent add column if not exists auth_user_id uuid;

create unique index if not exists idx_agent_auth_user_id
  on core.agent (auth_user_id);

-- ------------------------------------------------------------
-- 2. Guard the stage-sync trigger.
--
--    The original body updated lead.current_stage unconditionally,
--    so it wrote in INSERT order rather than changed_at order. A
--    backdated transition (bulk load, correction, concurrent write)
--    would leave current_stage pointing at an older stage than the
--    log says. Since this is an AFTER INSERT trigger the new row is
--    already visible, so max(changed_at) includes it: the guard is
--    true exactly when NEW is the newest transition for that lead.
--
--    `set search_path = ''` also clears the Supabase advisor's
--    mutable-search_path warning; every reference below is
--    schema-qualified as that requires.
-- ------------------------------------------------------------
create or replace function core.sync_lead_stage() returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  update core.lead
     set current_stage = new.to_stage,
         updated_at    = now()
   where id = new.lead_id
     and new.changed_at >= (
       select max(t.changed_at)
       from core.lead_stage_transition t
       where t.lead_id = new.lead_id
     );
  return new;
end $$;

-- ------------------------------------------------------------
-- 3. Expose agency_id on the two analytics views that lacked it.
--
--    Dashboards read from analytics.* only, never from core. Without
--    agency_id these two views cannot be filtered per tenant, which
--    would force the API to reach into core and break the rule the
--    schema exists to enforce.
--
--    CREATE OR REPLACE requires the existing output columns to keep
--    their name, type and position, so agency_id is appended last.
-- ------------------------------------------------------------
create or replace view analytics.agent_response_time as
select l.agent_id,
       count(*) as leads,
       avg(fr.first_outbound - l.created_at)    as avg_first_response,
       percentile_cont(0.5) within group
         (order by fr.first_outbound - l.created_at) as median_first_response,
       count(*) filter (where fr.first_outbound is null) as never_answered,
       ag.agency_id
from core.lead l
join core.agent ag on ag.id = l.agent_id
left join lateral (
  select min(i.occurred_at) as first_outbound
  from core.interaction i
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
) fr on true
group by l.agent_id, ag.agency_id;

create or replace view analytics.listing_performance as
select li.id as listing_id,
       li.operation_type,
       p.city, p.neighborhood, p.property_type,
       li.asking_price,
       (select count(*) from events.property_view v where v.listing_id = li.id) as views,
       (select count(*) from core.lead ld where ld.listing_id = li.id)          as leads,
       (select count(*) from core.lead ld
         where ld.listing_id = li.id
           and exists (select 1 from core.lead_stage_transition t
                       where t.lead_id = ld.id and t.to_stage = 'VISITED'))     as visits,
       (select count(*) from core.lead ld
         where ld.listing_id = li.id and ld.current_stage = 'WON')              as won,
       ag.agency_id
from core.listing li
join core.property p on p.id = li.property_id
join core.agent ag    on ag.id = li.agent_id;

-- ------------------------------------------------------------
-- 4. North Star metrics.
--
--    Four of the five target metrics are per-lead facts that none of
--    the three original views expose: time to first response, whether
--    a follow-up exists, whether the lead reached a visit, and whether
--    it died inside 48h. One row per lead, aggregate as needed.
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
        and lt.lost_at - l.created_at <= interval '48 hours')      as lost_within_48h
from core.lead l
join core.agent ag      on ag.id = l.agent_id
join core.lead_stage ls on ls.code = l.current_stage
left join lateral (
  select min(i.occurred_at) as first_outbound
  from core.interaction i
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
) fr on true
left join lateral (
  select min(t.changed_at) as lost_at
  from core.lead_stage_transition t
  where t.lead_id = l.id and t.to_stage = 'LOST'
) lt on true;

--    Stage-to-stage conversion. Counts DISTINCT leads that ever
--    reached each stage, so a lead bouncing back and forth is counted
--    once. LOST (sort_order 6) sits off the funnel: it is reported as
--    its own row but never used as a denominator, and because it
--    sorts last it is never the lag() of anything.
create or replace view analytics.stage_conversion as
with reached as (
  select distinct ag.agency_id, t.lead_id, t.to_stage as stage
  from core.lead_stage_transition t
  join core.lead l   on l.id  = t.lead_id
  join core.agent ag on ag.id = l.agent_id
),
counts as (
  select r.agency_id, r.stage, s.sort_order, count(*) as leads_reached
  from reached r
  join core.lead_stage s on s.code = r.stage
  group by r.agency_id, r.stage, s.sort_order
)
select agency_id,
       stage,
       sort_order,
       leads_reached,
       case when stage <> 'LOST' then
         lag(leads_reached) over (partition by agency_id order by sort_order)
       end as leads_prev_stage,
       case when stage <> 'LOST' then
         round(100.0 * leads_reached / nullif(
           lag(leads_reached) over (partition by agency_id order by sort_order), 0), 2)
       end as pct_from_prev
from counts;

-- ------------------------------------------------------------
-- 5. Missing indexes.
--    agent(agency_id) is the hop every tenancy check makes — every
--    authorized query in the API joins through it.
-- ------------------------------------------------------------
create index if not exists idx_agent_agency   on core.agent (agency_id);
create index if not exists idx_listing_agent  on core.listing (agent_id);
create index if not exists idx_listing_status on core.listing (status);
create index if not exists idx_property_owner on core.property (owner_id);
create index if not exists idx_task_agent_due on core.follow_up_task (agent_id, status, due_at);

-- ------------------------------------------------------------
-- 6. Cosmetic: silence the anon-callable advisor flag on the
--    platform's RLS event-trigger helper. It only acts on `public`
--    and explicitly skips our schemas, so this changes no behaviour.
-- ------------------------------------------------------------
do $$
begin
  if exists (select 1 from pg_proc p
             join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and p.proname = 'rls_auto_enable') then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end $$;

-- ============================================================
-- 007_ai_agents.sql — service accounts for AI agents (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: an AI agent needs to use the API across every agency with
-- roughly an agent's write powers, but a plain core.agent row can only
-- reach one agency (agency_id NOT NULL, auth_user_id UNIQUE), has no
-- limits on what it may write, and any OUTBOUND interaction it logs
-- would stop the "time to first agent response" clock — the #1 North
-- Star metric. docs/DECISIONS.md §17.
--
-- Shape:
--   * core.service_account — the credential (one Supabase Auth user),
--     its scopes, an hourly write budget and an on/off switch.
--   * one core.agent row per agency with role = 'AI_AGENT' pointing at
--     the service account. The API resolves a service-account token plus
--     an X-Agency-Id header to that agency's bot row, so every existing
--     agency filter applies unchanged. Bot rows never carry auth_user_id.
--   * ONE definition of "an agent response", applied to both
--     response-time views and the inactivity sweep: an OUTBOUND MESSAGE
--     or CALL written by a human. Notes, stage-change notes and anything
--     an AI agent wrote are on the timeline but do not stop the clock.
--     This also fixes the 005 sweep, whose own auto-note used to count
--     as the agent answering at hour 72.
-- ============================================================

-- ------------------------------------------------------------
-- 1. The credential.
-- ------------------------------------------------------------
create table if not exists core.service_account (
  id                 uuid primary key default gen_random_uuid(),
  name               text not null unique,
  -- Supabase auth.users.id of the bot's login. NOT NULL: a service account
  -- with no login is meaningless, unlike a seeded human agent.
  auth_user_id       uuid not null unique,
  -- What the bot may write. Reads need no scope: they are agency-scoped
  -- like everyone else's. Checked by app/deps.require_scope.
  scopes             text[] not null
                     default '{leads:create,leads:transition,interactions:write,tasks:write,visits:request}',
  -- Writes per rolling hour (transitions + interactions) before the API
  -- answers 429. A brake on runaway loops, not an exact quota.
  hourly_write_limit integer not null default 300 check (hourly_write_limit > 0),
  -- The kill switch: false -> 403 on the bot's next request, everywhere.
  active             boolean not null default true,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 2. Bot rows in core.agent.
--
-- role gains 'AI_AGENT'. The CHECK was auto-named agent_role_check by
-- 001; look it up by definition rather than trust the name.
-- ------------------------------------------------------------
do $$
declare
  v_name text;
begin
  select conname into v_name
    from pg_constraint
   where conrelid = 'core.agent'::regclass
     and contype = 'c'
     and pg_get_constraintdef(oid) like '%role%';
  if v_name is not null and v_name <> 'agent_role_check' then
    execute format('alter table core.agent rename constraint %I to agent_role_check', v_name);
  end if;
end $$;

alter table core.agent drop constraint if exists agent_role_check;
alter table core.agent add constraint agent_role_check
  check (role in ('AGENT','TEAM_ADMIN','AI_AGENT'));

alter table core.agent
  add column if not exists service_account_id uuid references core.service_account(id);

-- A bot row is exactly a row with a service account, and it never has a
-- login of its own: the credential lives on the service account.
alter table core.agent drop constraint if exists agent_ai_agent_check;
alter table core.agent add constraint agent_ai_agent_check
  check ((role = 'AI_AGENT') = (service_account_id is not null)
         and (role <> 'AI_AGENT' or auth_user_id is null));

-- At most one bot row per (service account, agency), so the API's lookup
-- of "this account's row for this agency" is a scalar_one_or_none().
create unique index if not exists idx_agent_service_account_agency
  on core.agent (service_account_id, agency_id)
  where service_account_id is not null;

-- ------------------------------------------------------------
-- 3. What counts as an agent response.
--
-- first_outbound was min(occurred_at) over every OUTBOUND interaction,
-- whatever its type and whoever wrote it. Now: OUTBOUND, type MESSAGE
-- or CALL, and not written by an AI_AGENT. Output columns are unchanged,
-- so CREATE OR REPLACE is allowed.
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
  left join core.agent b on b.id = i.created_by
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
    and i.type in ('MESSAGE','CALL')
    and b.role is distinct from 'AI_AGENT'
) fr on true
group by l.agent_id, ag.agency_id;

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
  left join core.agent b on b.id = i.created_by
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
    and i.type in ('MESSAGE','CALL')
    and b.role is distinct from 'AI_AGENT'
) fr on true
left join lateral (
  select min(t.changed_at) as lost_at
  from core.lead_stage_transition t
  where t.lead_id = l.id and t.to_stage = 'LOST'
) lt on true;

-- ------------------------------------------------------------
-- 4. The inactivity sweep uses the same definition. Its own NOTE no
--    longer counts as contact, so a lead nobody has actually answered
--    keeps being reported as unanswered (the PENDING-task guard still
--    stops it being re-flagged while a follow-up is open). Body
--    otherwise identical to 005.
-- ------------------------------------------------------------
create or replace function core.sweep_inactive_leads(p_hours integer default 72)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_cutoff timestamptz := now() - make_interval(hours => p_hours);
  v_count  integer;
begin
  with stale as (
    select l.id as lead_id, l.agent_id, a.agency_id
    from core.lead l
    join core.lead_stage s on s.code = l.current_stage
    join core.agent a      on a.id   = l.agent_id
    where not s.is_terminal
      and l.created_at < v_cutoff
      and coalesce(
            (select max(i.occurred_at) from core.interaction i
              left join core.agent b on b.id = i.created_by
              where i.lead_id = l.id and i.direction = 'OUTBOUND'
                and i.type in ('MESSAGE','CALL')
                and b.role is distinct from 'AI_AGENT'),
            '-infinity'::timestamptz) < v_cutoff
      and not exists (select 1 from core.follow_up_task f
                       where f.lead_id = l.id and f.status = 'PENDING')
    order by l.created_at
    limit 500
  ),
  t as (
    insert into core.follow_up_task (lead_id, agent_id, due_at, note, status)
    select lead_id, agent_id, now() + interval '24 hours',
           format('Auto-raised: no outbound contact in %sh.', p_hours), 'PENDING'
    from stale
    returning lead_id
  ),
  n as (
    insert into core.interaction (lead_id, direction, channel, type, body, occurred_at, created_by)
    select lead_id, 'OUTBOUND', 'IN_APP', 'NOTE',
           format('Lead flagged inactive after %sh with no outbound contact.', p_hours),
           now(), agent_id
    from stale
    returning 1
  ),
  e as (
    insert into events.domain_event (event_type, aggregate_type, aggregate_id, agency_id, payload)
    select 'lead.went_cold', 'lead', lead_id, agency_id,
           jsonb_build_object('inactivity_hours', p_hours)
    from stale
    returning 1
  )
  select count(*) into v_count from t;
  return v_count;
end $$;

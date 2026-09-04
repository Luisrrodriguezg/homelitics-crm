-- ============================================================
-- Real Estate CRM MVP — schema v2 (simple)
-- Idempotent: safe to re-run, it wipes and recreates everything.
-- Paste the whole file into Supabase SQL Editor and Run.
-- ============================================================

drop schema if exists pii cascade;
drop schema if exists core cascade;
drop schema if exists events cascade;
drop schema if exists analytics cascade;

create schema pii;
create schema core;
create schema events;
create schema analytics;

create extension if not exists pgcrypto;  -- gen_random_uuid()

-- ============================================================
-- pii
-- ============================================================
create table pii.person (
  id            uuid primary key default gen_random_uuid(),
  full_name     text not null,
  email         text,
  phone         text,
  national_id   text,
  anonymized_at timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ============================================================
-- core: org & roles
-- ============================================================
create table core.agency (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  created_at timestamptz not null default now()
);

create table core.agent (
  id         uuid primary key default gen_random_uuid(),
  person_id  uuid not null references pii.person(id),
  agency_id  uuid not null references core.agency(id),
  role       text not null default 'AGENT' check (role in ('AGENT','TEAM_ADMIN')),
  active     boolean not null default true,
  auth_user_id uuid,   -- Supabase auth.users.id; populated by scripts/bind_agents.py
  created_at timestamptz not null default now()
);

-- most agents are unbound (null); Postgres allows repeated nulls under a unique index
create unique index idx_agent_auth_user_id on core.agent (auth_user_id);

create table core.owner (
  id         uuid primary key default gen_random_uuid(),
  person_id  uuid not null references pii.person(id),
  created_at timestamptz not null default now()
);

create table core.client (
  id         uuid primary key default gen_random_uuid(),
  person_id  uuid not null references pii.person(id),
  created_at timestamptz not null default now()
);

-- ============================================================
-- core: inventory
-- ============================================================
create table core.property (
  id            uuid primary key default gen_random_uuid(),
  owner_id      uuid not null references core.owner(id),
  property_type text not null check (property_type in ('APARTMENT','HOUSE','STUDIO','COUNTRY_HOUSE')),
  city          text not null,
  neighborhood  text not null,
  address       text not null,
  area_m2       numeric(8,2) not null check (area_m2 > 0),
  bedrooms      smallint not null,
  bathrooms     smallint not null,
  parking_spots smallint not null default 0,
  year_built    smallint,
  hoa_fee       numeric(15,2),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table core.listing (
  id                   uuid primary key default gen_random_uuid(),
  property_id          uuid not null references core.property(id),
  agent_id             uuid not null references core.agent(id),
  operation_type       text not null check (operation_type in ('SALE','RENT')),
  asking_price         numeric(15,2) not null check (asking_price > 0),
  min_acceptable_price numeric(15,2) not null,
  status               text not null default 'ACTIVE' check (status in ('ACTIVE','PAUSED','CLOSED')),
  published_at         timestamptz not null default now(),
  closed_at            timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  check (min_acceptable_price <= asking_price)
);

-- ============================================================
-- core: funnel
-- ============================================================
create table core.lead_stage (
  code        text primary key,
  label       text not null,
  sort_order  smallint not null,
  is_terminal boolean not null default false
);

insert into core.lead_stage values
  ('INTERESTED','Interested',1,false),
  ('VISIT_SCHEDULED','Visit scheduled',2,false),
  ('VISITED','Visited',3,false),
  ('NEGOTIATING','Negotiating',4,false),
  ('WON','Won',5,true),
  ('LOST','Lost',6,true);

create table core.lost_reason (
  id   uuid primary key default gen_random_uuid(),
  code text not null unique
);

insert into core.lost_reason (code) values
  ('PRICE'),('LOCATION'),('BOUGHT_ELSEWHERE'),('NO_RESPONSE'),('FINANCING'),('OTHER');

create table core.lead (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid not null references core.client(id),
  listing_id     uuid not null references core.listing(id),
  agent_id       uuid not null references core.agent(id),
  source_channel text not null check (source_channel in ('WHATSAPP','IN_APP','CALL')),
  current_stage  text not null default 'INTERESTED' references core.lead_stage(code),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (client_id, listing_id)
);

create index idx_lead_agent   on core.lead (agent_id);
create index idx_lead_listing on core.lead (listing_id);

create table core.lead_stage_transition (
  id         uuid primary key default gen_random_uuid(),
  lead_id    uuid not null references core.lead(id),
  from_stage text references core.lead_stage(code),
  to_stage   text not null references core.lead_stage(code),
  changed_by uuid references core.agent(id),
  changed_at timestamptz not null default now()
);

create index idx_transition_lead on core.lead_stage_transition (lead_id, changed_at);

-- the one piece of logic: keep lead.current_stage in sync with the log
create function core.sync_lead_stage() returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  -- AFTER INSERT, so NEW is already visible to this max(): the guard holds
  -- exactly when NEW is the newest transition for the lead. A backdated or
  -- concurrent transition is still logged, it just no longer clobbers the cache.
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

create trigger trg_sync_lead_stage
  after insert on core.lead_stage_transition
  for each row execute function core.sync_lead_stage();

create table core.lead_lost_detail (
  lead_id        uuid primary key references core.lead(id),
  lost_reason_id uuid not null references core.lost_reason(id),
  free_text      text,
  created_at     timestamptz not null default now()
);

-- ============================================================
-- core: activity
-- ============================================================
create table core.interaction (
  id          uuid primary key default gen_random_uuid(),
  lead_id     uuid not null references core.lead(id),
  direction   text not null check (direction in ('INBOUND','OUTBOUND')),
  channel     text not null check (channel in ('WHATSAPP','IN_APP','CALL')),
  type        text not null default 'MESSAGE' check (type in ('MESSAGE','CALL','NOTE','STATUS_CHANGE')),
  body        text,
  occurred_at timestamptz not null default now(),
  created_by  uuid references core.agent(id)
);

create index idx_interaction_lead on core.interaction (lead_id, occurred_at);

create table core.appointment (
  id           uuid primary key default gen_random_uuid(),
  lead_id      uuid not null references core.lead(id),
  agent_id     uuid not null references core.agent(id),
  scheduled_at timestamptz not null,
  duration_min smallint not null default 60,
  status       text not null default 'PENDING_CONFIRMATION'
               check (status in ('PENDING_CONFIRMATION','CONFIRMED','RESCHEDULED','CANCELLED','COMPLETED','NO_SHOW')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index idx_appointment_lead  on core.appointment (lead_id);
create index idx_appointment_agent on core.appointment (agent_id, scheduled_at);

create table core.objection (
  id   uuid primary key default gen_random_uuid(),
  code text not null unique
);

insert into core.objection (code) values
  ('PRICE'),('SIZE'),('LOCATION'),('CONDITION'),('HOA_FEE'),('OTHER');

create table core.visit_feedback (
  id                uuid primary key default gen_random_uuid(),
  appointment_id    uuid not null references core.appointment(id),
  submitted_by      text not null check (submitted_by in ('AGENT','CLIENT')),
  interest_score    smallint check (interest_score between 1 and 5),
  objection_id      uuid references core.objection(id),
  close_probability numeric(3,2) check (close_probability between 0 and 1),
  free_text         text,
  created_at        timestamptz not null default now()
);

create table core.follow_up_task (
  id         uuid primary key default gen_random_uuid(),
  lead_id    uuid not null references core.lead(id),
  agent_id   uuid not null references core.agent(id),
  due_at     timestamptz not null,
  note       text,
  status     text not null default 'PENDING' check (status in ('PENDING','DONE','SNOOZED')),
  created_at timestamptz not null default now()
);

create table core.assignment_audit (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid not null references core.lead(id),
  from_agent_id uuid not null references core.agent(id),
  to_agent_id   uuid not null references core.agent(id),
  reassigned_by uuid not null references core.agent(id),
  reassigned_at timestamptz not null default now()
);

-- ============================================================
-- core: money
-- ============================================================
create table core.offer (
  id         uuid primary key default gen_random_uuid(),
  lead_id    uuid not null references core.lead(id),
  amount     numeric(15,2) not null check (amount > 0),
  offered_by text not null check (offered_by in ('CLIENT','AGENT')),
  status     text not null default 'PENDING' check (status in ('PENDING','ACCEPTED','REJECTED','COUNTERED')),
  offered_at timestamptz not null default now()
);

create table core.deal (
  lead_id         uuid primary key references core.lead(id),
  closed_amount   numeric(15,2) not null check (closed_amount > 0),
  commission      numeric(15,2) not null default 0,
  closed_at       timestamptz not null,
  contract_start  date,
  contract_months smallint
);

-- ============================================================
-- events
-- ============================================================
create table events.property_view (
  id         uuid primary key default gen_random_uuid(),
  listing_id uuid not null references core.listing(id),
  client_id  uuid references core.client(id),
  session_id text not null,
  viewed_at  timestamptz not null default now()
);

create index idx_view_listing on events.property_view (listing_id, viewed_at);

-- tenancy + hot-path indexes. agent(agency_id) is the hop every authorized query makes.
create index idx_agent_agency   on core.agent (agency_id);
create index idx_listing_agent  on core.listing (agent_id);
create index idx_listing_status on core.listing (status);
create index idx_property_owner on core.property (owner_id);
create index idx_task_agent_due on core.follow_up_task (agent_id, status, due_at);

-- ============================================================
-- analytics: plain views (always fresh, no refresh needed)
-- ============================================================
create view analytics.funnel_daily as
select date_trunc('day', t.changed_at)::date as day,
       ag.agency_id,
       t.to_stage,
       count(*) as transitions
from core.lead_stage_transition t
join core.lead l   on l.id = t.lead_id
join core.agent ag on ag.id = l.agent_id
group by 1, 2, 3;

-- Every analytics view exposes agency_id: dashboards read from analytics.* only,
-- never from core, so per-tenant filtering has to be possible here.
create view analytics.agent_response_time as
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

create view analytics.listing_performance as
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

-- One row per lead, carrying the four North Star facts that none of the
-- views above expose: time to first response, whether a follow-up exists,
-- whether the lead reached a visit, and whether it died inside 48h.
create view analytics.lead_outcome as
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

-- Stage-to-stage conversion. DISTINCT leads that ever reached each stage, so a
-- lead bouncing back and forth counts once. LOST (sort_order 6) is off-funnel:
-- reported as its own row, never a denominator, and because it sorts last it is
-- never the lag() of anything.
create view analytics.stage_conversion as
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

-- ============================================================
-- 003_availability.sql — HU-05 agent availability
-- ============================================================

create table if not exists core.agent_availability (
  id         uuid primary key default gen_random_uuid(),
  agent_id   uuid not null references core.agent(id) on delete cascade,
  weekday    smallint not null check (weekday between 0 and 6),
  start_time time not null,
  end_time   time not null,
  valid_from date not null default current_date,
  valid_to   date,
  created_at timestamptz not null default now(),
  check (start_time < end_time),
  check (valid_to is null or valid_to >= valid_from)
);

create table if not exists core.agent_time_off (
  id         uuid primary key default gen_random_uuid(),
  agent_id   uuid not null references core.agent(id) on delete cascade,
  starts_at  timestamptz not null,
  ends_at    timestamptz not null,
  reason     text,
  created_at timestamptz not null default now(),
  check (starts_at < ends_at)
);

create index if not exists idx_agent_availability_agent
  on core.agent_availability (agent_id, weekday);
create index if not exists idx_agent_time_off_agent
  on core.agent_time_off (agent_id, starts_at, ends_at);

-- ============================================================
-- 004_events_outbox.sql — domain-event outbox + surgical Realtime grant
-- ============================================================

create table if not exists events.domain_event (
  id             bigint generated always as identity primary key,
  event_type     text not null,
  aggregate_type text not null,
  aggregate_id   uuid not null,
  agency_id      uuid not null,
  payload        jsonb not null default '{}',
  occurred_at    timestamptz not null default now(),
  published_at   timestamptz,
  attempts       integer not null default 0
);

create index if not exists idx_domain_event_unpublished
  on events.domain_event (occurred_at)
  where published_at is null;

do $$
begin
  if exists (select 1 from information_schema.schemata where schema_name = 'auth') then

    create or replace function core.current_agency_id() returns uuid
    language sql
    stable
    security definer
    set search_path = ''
    as $fn$
      select a.agency_id
      from core.agent a
      where a.auth_user_id = auth.uid()
      limit 1
    $fn$;

    revoke execute on function core.current_agency_id() from public, anon;
    grant execute on function core.current_agency_id() to authenticated;

    grant usage on schema events to authenticated;
    grant select on events.domain_event to authenticated;

    execute 'alter table events.domain_event enable row level security';

    if not exists (
      select 1 from pg_policies
      where schemaname = 'events' and tablename = 'domain_event'
        and policyname = 'domain_event_own_agency'
    ) then
      execute $pol$
        create policy domain_event_own_agency on events.domain_event
          for select to authenticated
          using (agency_id = core.current_agency_id())
      $pol$;
    end if;

    if not exists (
      select 1 from pg_publication_tables
      where pubname = 'supabase_realtime'
        and schemaname = 'events' and tablename = 'domain_event'
    ) then
      execute 'alter publication supabase_realtime add table events.domain_event';
    end if;

  end if;
end $$;

-- ============================================================
-- 005_cron_jobs.sql — background jobs as SQL + pg_cron schedules
-- ============================================================

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
              where i.lead_id = l.id and i.direction = 'OUTBOUND'),
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

create or replace function events.relay_domain_events(p_batch integer default 100)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_count integer;
begin
  with batch as (
    select e.id, e.event_type, e.aggregate_id
    from events.domain_event e
    where e.published_at is null
    order by e.occurred_at, e.id
    limit p_batch
    for update skip locked
  ),
  first_touch as (
    insert into core.follow_up_task (lead_id, agent_id, due_at, note, status)
    select l.id, l.agent_id, now() + interval '24 hours',
           'Auto-raised: first-touch follow-up for a new lead.', 'PENDING'
    from batch b
    join core.lead l on l.id = b.aggregate_id
    where b.event_type = 'lead.created'
      and not exists (select 1 from core.follow_up_task f
                       where f.lead_id = l.id and f.status = 'PENDING')
    returning 1
  ),
  done as (
    update events.domain_event e
       set published_at = now()
      from batch b
     where e.id = b.id
    returning 1
  )
  select count(*) into v_count from done;
  return v_count;
end $$;

do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'pg_cron') then

    create extension if not exists pg_cron with schema pg_catalog;
    grant usage on schema cron to postgres;
    grant all privileges on all tables in schema cron to postgres;

    perform cron.unschedule(jobid)
      from cron.job
     where jobname in ('homelitics_sweep', 'homelitics_relay');

    perform cron.schedule(
      'homelitics_sweep', '0 * * * *',
      $job$ select core.sweep_inactive_leads(72) $job$
    );
    perform cron.schedule(
      'homelitics_relay', '30 seconds',
      $job$ select events.relay_domain_events() $job$
    );

  end if;
end $$;

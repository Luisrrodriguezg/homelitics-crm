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
  created_at timestamptz not null default now()
);

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
language plpgsql as $$
begin
  update core.lead
     set current_stage = new.to_stage, updated_at = now()
   where id = new.lead_id;
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

create view analytics.agent_response_time as
select l.agent_id,
       count(*) as leads,
       avg(fr.first_outbound - l.created_at)    as avg_first_response,
       percentile_cont(0.5) within group
         (order by fr.first_outbound - l.created_at) as median_first_response,
       count(*) filter (where fr.first_outbound is null) as never_answered
from core.lead l
left join lateral (
  select min(i.occurred_at) as first_outbound
  from core.interaction i
  where i.lead_id = l.id and i.direction = 'OUTBOUND'
) fr on true
group by l.agent_id;

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
         where ld.listing_id = li.id and ld.current_stage = 'WON')              as won
from core.listing li
join core.property p on p.id = li.property_id;

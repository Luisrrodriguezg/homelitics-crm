-- ============================================================
-- 003_availability.sql — HU-05 agent availability (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- CLAUDE.md cut "agent availability tables" so HU-02 became
-- "request a visit -> agent confirms". HU-05 ("an agent publishes
-- when they are reachable") still needs somewhere to store that.
-- These two tables are the minimum: a weekly rule set plus ad-hoc
-- time off. Slot computation stays in the API (services/availability).
-- ============================================================

-- Weekly recurring availability. One row per (agent, weekday, block).
-- weekday: 0 = Monday .. 6 = Sunday (Python date.weekday()).
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

-- Ad-hoc time off (vacation, sick, personal). Half-open [starts_at, ends_at).
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

-- ============================================================
-- 004_events_outbox.sql — domain-event outbox (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- CLAUDE.md cut Kafka: "notifications are sent synchronously in the
-- request that triggers them". The outbox keeps that promise while
-- still giving us a durable event log and a Realtime feed:
--
--   * the API inserts the event row IN THE CALLER'S TRANSACTION,
--     so an event exists iff the business fact committed;
--   * a 30s in-process relay (app/jobs.py) dispatches unpublished
--     rows to in-process handlers and stamps published_at;
--   * Supabase Realtime streams the same table to the dashboard.
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

-- The relay only ever scans unpublished rows.
create index if not exists idx_domain_event_unpublished
  on events.domain_event (occurred_at)
  where published_at is null;

-- ------------------------------------------------------------
-- Realtime access — deliberately surgical.
--
-- Today `authenticated` has USAGE on NONE of our schemas; that is
-- exactly what makes service-layer authorization safe (a forgotten
-- filter cannot leak because the role cannot reach the table at
-- all). Opening Realtime must not undo that. So we grant the
-- narrowest thing that works: USAGE on `events`, SELECT on this one
-- table, RLS on this one table, one policy keyed to the caller's
-- agency. `core`, `analytics`, `pii` and every other `events` object
-- stay closed.
--
-- Guarded behind an `auth` schema check so this file also applies to
-- the Phase 6 local Postgres container, which has no Supabase auth.
-- ------------------------------------------------------------
do $$
begin
  if exists (select 1 from information_schema.schemata where schema_name = 'auth') then

    -- security definer: reads core.agent, which `authenticated` cannot.
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

    -- Add to the Realtime publication if not already a member.
    if not exists (
      select 1 from pg_publication_tables
      where pubname = 'supabase_realtime'
        and schemaname = 'events' and tablename = 'domain_event'
    ) then
      execute 'alter publication supabase_realtime add table events.domain_event';
    end if;

  end if;
end $$;

-- ============================================================
-- 005_cron_jobs.sql — background jobs as SQL + pg_cron schedules (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: the two background jobs (inactivity sweep, outbox relay) used to
-- live in an in-process APScheduler on the FastAPI lifespan. That was the
-- one thing forcing the API onto an always-on box. Moving the jobs into
-- Postgres lets the container sleep on a free scale-to-zero host while
-- the funnel keeps ticking. docs/DECISIONS.md §14.
--
-- Two parts:
--   1. the job bodies as plain SQL functions — the single implementation.
--      app/jobs.py now just calls them, so the local compose profile
--      (plain postgres:17-alpine, no pg_cron) runs the same code.
--   2. pg_cron schedules — guarded so the file also applies where pg_cron
--      is not available (the local container).
-- ============================================================

-- ------------------------------------------------------------
-- 1a. Inactivity sweep.
--
-- For every non-terminal lead older than p_hours with no OUTBOUND
-- interaction inside p_hours and no PENDING follow-up: raise a follow-up
-- task (due +24h), log an OUTBOUND NOTE, emit `lead.went_cold`.
-- Idempotent within one runner: the PENDING-task guard skips leads already
-- flagged, so running it twice in an hour does not double up. Capped at
-- 500 leads per run so a cold start on a big backlog stays bounded.
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

-- ------------------------------------------------------------
-- 1b. Outbox relay.
--
-- Takes up to p_batch unpublished events, oldest first, runs the handler
-- for each type, stamps published_at. Today the only handler is
-- `lead.created` -> first-touch follow-up (HU-10). Events with no handler
-- are simply stamped. `for update skip locked` means two runners (pg_cron
-- and a stray in-process scheduler, say) can never dispatch the same row.
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 2. pg_cron schedules — Supabase only.
--
-- Guarded on pg_cron being *available*: the local postgres:17-alpine image
-- does not ship it, so there the functions exist and app/jobs.py drives
-- them instead. On Supabase the two grants are what the Supabase docs ask
-- for after enabling the extension. Re-running unschedules and re-creates
-- the two jobs, so edits to the schedule land cleanly.
--
-- The sweep threshold (72h) mirrors INACTIVITY_HOURS. Change both together.
-- ------------------------------------------------------------
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
      'homelitics_relay', '*/2 * * * *',
      $job$ select events.relay_domain_events() $job$
    );

  end if;
end $$;

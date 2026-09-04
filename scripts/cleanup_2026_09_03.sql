-- ============================================================================
-- Phase 0 — live-data cleanup for `homelitics` (2026-09-03)
-- Paste the WHOLE file into Supabase → SQL Editor → Run.  Idempotent.
--
--   step 1  purge pytest-* debris   (scripts/purge_test_rows.sql, verbatim)
--   step 2  three small repairs     (1 + 3 + 14 rows expected)
--   step 3  default weekly availability for every real agent (480 rows)
--   step 4  post-check              (expect debris 0, wrong_calendar 0, early 0, availability 480)
-- ============================================================================

-- ---------------------------------------------------------------- step 1
-- ============================================================
-- purge_test_rows.sql — delete everything tests/conftest.py made.
--
-- The fixture tags every row it creates with a `pytest-<hex>` prefix on the
-- agency name, the person's full_name and the property's city. This reclaims
-- them all, children first.
--
-- Why it exists: the teardown in conftest.py deletes by id lists collected
-- during the fixture. If a run dies mid-fixture (or the event loop tears down
-- early) those lists are lost and the rows are orphaned in the live database --
-- which is exactly what happened to run `pytest-44720aa9`.
--
-- ONE statement (a single DO block), so it runs unchanged from psql, the
-- Supabase SQL Editor, or SQLAlchemy's exec_driver_sql.
--
-- Idempotent and safe on a fully seeded database: nothing outside the
-- `pytest-` prefix is reachable from here.
-- ============================================================
do $$
declare
  v_person  uuid[];
  v_agency  uuid[];
  v_agent   uuid[];
  v_client  uuid[];
  v_prop    uuid[];
  v_listing uuid[];
  v_lead    uuid[];
  v_appt    uuid[];
begin
  select coalesce(array_agg(id), '{}') into v_person
    from pii.person where full_name like 'pytest-%';
  select coalesce(array_agg(id), '{}') into v_agency
    from core.agency where name like 'pytest-%';

  select coalesce(array_agg(id), '{}') into v_agent
    from core.agent where agency_id = any(v_agency) or person_id = any(v_person);
  select coalesce(array_agg(id), '{}') into v_client
    from core.client where person_id = any(v_person);

  -- city is tagged too, so a property survives even if its owner person was
  -- already reclaimed by a previous partial purge.
  select coalesce(array_agg(p.id), '{}') into v_prop
    from core.property p
    left join core.owner o on o.id = p.owner_id
   where o.person_id = any(v_person) or p.city like 'pytest-%';

  select coalesce(array_agg(id), '{}') into v_listing
    from core.listing where property_id = any(v_prop) or agent_id = any(v_agent);

  -- listing_id and agent_id are both indexed; client_id alone is not, and the
  -- fixture never attaches a lead to a listing it did not create, so adding it
  -- would buy nothing and cost a sequential scan of core.lead.
  select coalesce(array_agg(id), '{}') into v_lead
    from core.lead
   where listing_id = any(v_listing)
      or agent_id   = any(v_agent);

  select coalesce(array_agg(id), '{}') into v_appt
    from core.appointment where lead_id = any(v_lead);

  delete from core.visit_feedback        where appointment_id = any(v_appt);
  delete from core.appointment           where lead_id = any(v_lead);
  delete from core.offer                 where lead_id = any(v_lead);
  delete from core.deal                  where lead_id = any(v_lead);
  delete from core.follow_up_task        where lead_id = any(v_lead);
  delete from core.assignment_audit      where lead_id = any(v_lead);
  delete from core.lead_lost_detail      where lead_id = any(v_lead);
  delete from core.interaction           where lead_id = any(v_lead);
  delete from core.lead_stage_transition where lead_id = any(v_lead);
  delete from core.lead                  where id = any(v_lead);

  -- ONLY by listing_id, which idx_view_listing covers. The `or client_id` this
  -- replaced forced a sequential scan of all 375k property_view rows -- fine
  -- once, ruinous when it ran on every test teardown.
  delete from events.property_view where listing_id = any(v_listing);
  delete from core.listing  where id = any(v_listing);
  delete from core.property where id = any(v_prop);
  delete from core.client   where id = any(v_client);
  delete from core.owner    where person_id = any(v_person);
  delete from core.agent    where id = any(v_agent);
  delete from core.agency   where id = any(v_agency);
  delete from pii.person    where id = any(v_person);

  raise notice 'purged: % persons, % agencies, % agents, % clients, % properties, % listings, % leads',
    coalesce(array_length(v_person,1),0),  coalesce(array_length(v_agency,1),0),
    coalesce(array_length(v_agent,1),0),   coalesce(array_length(v_client,1),0),
    coalesce(array_length(v_prop,1),0),    coalesce(array_length(v_listing,1),0),
    coalesce(array_length(v_lead,1),0);
end $$;

-- ---------------------------------------------------------------- step 2
-- a) a reassigned lead's still-blocking appointment follows the lead (history rows stay)
update core.appointment a
   set agent_id = l.agent_id, updated_at = now()
  from core.lead l
 where l.id = a.lead_id and a.agent_id <> l.agent_id
   and a.status in ('PENDING_CONFIRMATION','CONFIRMED','RESCHEDULED');

-- b) LOST leads cannot have a live visit
update core.appointment a
   set status = 'CANCELLED', updated_at = now()
  from core.lead l
 where l.id = a.lead_id and l.current_stage = 'LOST'
   and a.status in ('PENDING_CONFIRMATION','CONFIRMED','RESCHEDULED');

-- c) no interaction predates its lead (seeder rounding, 14 rows)
update core.interaction i
   set occurred_at = l.created_at
  from core.lead l
 where l.id = i.lead_id and i.occurred_at < l.created_at;

-- ---------------------------------------------------------------- step 3
-- Mon–Fri 09:00–12:00 and 14:00–18:00 (America/Bogota) for every active real agent
-- that has no rules yet. Agents can edit through /agents/{id}/availability.
insert into core.agent_availability (agent_id, weekday, start_time, end_time)
select a.id, d.weekday, b.start_time, b.end_time
  from core.agent a
  join core.agency g on g.id = a.agency_id
  cross join (values (0),(1),(2),(3),(4)) d(weekday)
  cross join (values ('09:00'::time,'12:00'::time), ('14:00'::time,'18:00'::time)) b(start_time,end_time)
 where a.active and g.name not like 'pytest-%'
   and not exists (select 1 from core.agent_availability v where v.agent_id = a.id);

-- ---------------------------------------------------------------- step 4
select (select count(*) from core.agency where name like 'pytest-%')                          as debris,
       (select count(*) from core.appointment a join core.lead l on l.id = a.lead_id
         where a.agent_id <> l.agent_id
           and a.status in ('PENDING_CONFIRMATION','CONFIRMED','RESCHEDULED'))                 as wrong_calendar,
       (select count(*) from core.interaction i join core.lead l on l.id = i.lead_id
         where i.occurred_at < l.created_at)                                                   as early,
       (select count(*) from core.agent_availability)                                          as availability,
       (select count(*) from core.agent)                                                       as agents,
       (select count(*) from core.agency)                                                      as agencies;

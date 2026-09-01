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

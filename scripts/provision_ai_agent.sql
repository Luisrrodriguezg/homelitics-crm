-- ============================================================
-- provision_ai_agent.sql — create (or repair) one AI-agent service
-- account and its per-agency AI_AGENT rows.
--
-- Prerequisite: migrations/007_ai_agents.sql applied, and a Supabase Auth
-- user for the bot created in the dashboard:
--   Authentication -> Users -> Add user -> "Create new user",
--   email e.g. ai-agent@homelitics.test, a long random password,
--   tick "Auto Confirm User". Copy the user's UUID into v_auth below.
--
-- Then paste this whole file into the SQL Editor and Run. Idempotent:
-- re-run it after every re-seed (both seeders TRUNCATE core.agent, which
-- drops the bot rows; core.service_account survives) and whenever an
-- agency is added. Existing rows are left alone.
--
-- The bot then authenticates with that email + password (see
-- docs/API_GUIDE.md §2d) and sends X-Agency-Id: <agency uuid> on every
-- request. The final SELECT prints the agency -> uuid list to hand over.
-- ============================================================
do $$
declare
  v_auth    uuid := '00000000-0000-0000-0000-000000000000';  -- <-- the Auth user's UUID
  v_name    text := 'ai-agent';                                -- one account per bot
  v_account uuid;
  v_person  uuid;
  v_made    integer;
begin
  if v_auth = '00000000-0000-0000-0000-000000000000' then
    raise exception 'Set v_auth to the bot''s Supabase Auth user id first';
  end if;

  insert into core.service_account (name, auth_user_id)
  values (v_name, v_auth)
  on conflict (name) do update set auth_user_id = excluded.auth_user_id,
                                   updated_at   = now()
  returning id into v_account;

  -- One pii.person shared by every bot row: it is not a human, but
  -- core.agent.person_id is NOT NULL and /me reads a name from it.
  -- Tagged with the account name so it is findable.
  select a.person_id into v_person
    from core.agent a
   where a.service_account_id = v_account
   limit 1;
  if v_person is null then
    insert into pii.person (full_name, email)
    values (format('AI Assistant (%s)', v_name), null)
    returning id into v_person;
  end if;

  insert into core.agent (person_id, agency_id, role, active, service_account_id)
  select v_person, g.id, 'AI_AGENT', true, v_account
    from core.agency g
   where g.name not like 'pytest-%'
     and not exists (select 1 from core.agent a
                      where a.service_account_id = v_account and a.agency_id = g.id);
  get diagnostics v_made = row_count;

  raise notice 'service account % (%): % new AI_AGENT row(s)', v_name, v_account, v_made;
end $$;

-- Hand this list to whoever configures the bot: X-Agency-Id per agency.
select g.name as agency, g.id as agency_id, a.id as ai_agent_id, a.active
  from core.agent a
  join core.agency g on g.id = a.agency_id
  join core.service_account s on s.id = a.service_account_id
 where a.role = 'AI_AGENT'
 order by g.name;

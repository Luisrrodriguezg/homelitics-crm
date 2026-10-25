-- ============================================================
-- provision_agency_bots.sql — one AI-agent service account per agency.
--
-- The shared `ai-agent` (provision_ai_agent.sql) has an AI_AGENT row in every
-- agency, so its token reaches any agency whose id it puts in X-Agency-Id.
-- Each account made here has exactly one row, in its own agency: any other
-- X-Agency-Id is a 403 (app/deps._resolve_service_account). No schema change —
-- 007 already allows an account with rows in only some agencies.
--
-- Prerequisite: the bot's Supabase Auth user, created in the dashboard
-- (Authentication -> Users -> Add user -> "Create new user", tick
-- "Auto Confirm User", a long random password) with the email in the list
-- below. The login is found by email; nobody copies a UUID.
--
-- Then paste this file into the SQL Editor and Run ("Run without RLS").
-- Idempotent: a bot whose login does not exist yet is skipped with a notice,
-- an existing account or row is left alone. Re-run after creating more
-- logins, and after every re-seed (both seeders TRUNCATE core.agent, which
-- drops the bot rows; core.service_account survives).
--
-- Agencies are matched by name, not id, so the list survives a re-seed.
-- ============================================================
do $$
declare
  b         record;
  v_auth    uuid;
  v_agency  uuid;
  v_account uuid;
  v_person  uuid;
begin
  for b in
    select * from (values
      ('ai-ramirez-plc',                    'bot@ramirez-plc.homelitics.test',                    'Ramírez PLC Realty'),
      ('ai-cruz-oviedo',                    'bot@cruz-oviedo.homelitics.test',                    'Cruz-Oviedo Realty'),
      ('ai-gonzalez-villamizar-and-vargas', 'bot@gonzalez-villamizar-and-vargas.homelitics.test', 'González, Villamizar and Vargas Realty'),
      ('ai-martinez-plc',                   'bot@martinez-plc.homelitics.test',                   'Martínez PLC Realty'),
      ('ai-pinto-llc',                      'bot@pinto-llc.homelitics.test',                      'Pinto LLC Realty'),
      ('ai-sanchez-hernandez-and-alvarez',  'bot@sanchez-hernandez-and-alvarez.homelitics.test',  'Sánchez, Hernández and Álvarez Realty')
    ) as t(name, email, agency)
  loop
    select id into v_auth from auth.users where lower(email) = b.email;
    if v_auth is null then
      raise notice '%: no Supabase user % yet, skipped', b.name, b.email;
      continue;
    end if;

    select id into v_agency from core.agency where name = b.agency;
    if v_agency is null then
      raise exception '%: agency "%" not found', b.name, b.agency;
    end if;

    -- The credential. Scopes = 007's defaults plus the two granted to
    -- ai-agent since (008 clients:create, 009 visits:feedback), which the
    -- Telegram flow needs. visits:manage and leads:close stay off.
    insert into core.service_account (name, auth_user_id, scopes)
    values (b.name, v_auth,
            '{leads:create,leads:transition,interactions:write,tasks:write,visits:request,clients:create,visits:feedback}')
    on conflict (name) do update set auth_user_id = excluded.auth_user_id,
                                     updated_at   = now()
    returning id into v_account;

    -- Its one AI_AGENT row. Own pii.person per account so /me names the bot.
    if not exists (select 1 from core.agent
                    where service_account_id = v_account and agency_id = v_agency) then
      insert into pii.person (full_name)
      values (format('AI Assistant (%s)', b.name))
      returning id into v_person;

      insert into core.agent (person_id, agency_id, role, active, service_account_id)
      values (v_person, v_agency, 'AI_AGENT', true, v_account);
    end if;

    raise notice '%: ready in %', b.name, b.agency;
  end loop;
end $$;

-- Every per-agency bot must show exactly one row. Hand the bot team its
-- agency_id (the X-Agency-Id it sends) along with the login.
select s.name, s.active as account_active, g.name as agency, g.id as agency_id,
       a.id as ai_agent_id, a.active as row_active,
       count(*) over (partition by s.id) as agencies
  from core.service_account s
  join core.agent a  on a.service_account_id = s.id
  join core.agency g on g.id = a.agency_id
 where s.name <> 'ai-agent'
 order by s.name;

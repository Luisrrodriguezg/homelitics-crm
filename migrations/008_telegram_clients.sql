-- ============================================================
-- 008_telegram_clients.sql — clients can be registered, and a Telegram
-- account identifies one (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: POST /leads needs an existing client_id and nothing could create
-- one, so the bot could only open leads for seeded clients. POST /clients
-- now creates them. A Telegram user id is the one stable key a client
-- arrives with — names, emails and phones all collide or get reformatted
-- in the live data — so a repeat Telegram contact is recognised by a
-- UNIQUE index, exactly as HU-01 dedups leads. docs/DECISIONS.md §18.
-- ============================================================

-- ------------------------------------------------------------
-- 1. The Telegram id lives on the person, not on core.client: it
--    identifies a human, and right-to-erasure is one UPDATE on
--    pii.person (which must null this column too). Telegram ids exceed
--    2^31, hence bigint. Nullable: most people never used the bot, and
--    Postgres treats NULLs as distinct under a unique index.
-- ------------------------------------------------------------
alter table pii.person add column if not exists telegram_user_id bigint;

create unique index if not exists uq_person_telegram_user_id
  on pii.person (telegram_user_id);

-- ------------------------------------------------------------
-- 2. The bot may register clients. New accounts get the scope by
--    default; existing ones (the live bot) are granted it here. The
--    guard makes a re-run a no-op.
-- ------------------------------------------------------------
alter table core.service_account alter column scopes
  set default '{leads:create,leads:transition,interactions:write,tasks:write,visits:request,clients:create}';

update core.service_account
   set scopes     = array_append(scopes, 'clients:create'),
       updated_at = now()
 where not ('clients:create' = any(scopes));

-- ============================================================
-- 012_agent_invites.sql — an invite is the only way to sign up
-- (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: public sign-up is off in Supabase Auth. A TEAM_ADMIN issues an
-- invite for their own agency with a fixed role (AGENT or TEAM_ADMIN);
-- whoever redeems it registers themselves and becomes an agent of that
-- agency with that role. Agency and role come from this row, never from
-- the person registering. docs/DECISIONS.md §21.
-- ============================================================

create table if not exists core.agent_invite (
  id                uuid primary key default gen_random_uuid(),
  agency_id         uuid not null references core.agency(id),
  -- AI agents are provisioned (scripts/provision_ai_agent.sql), never invited.
  role              text not null check (role in ('AGENT','TEAM_ADMIN')),
  -- Optional lock: when set, only this address (lower-cased) may redeem it.
  email             text,
  -- sha256 hex of the token. The token itself is shown once, to the admin
  -- who created the invite, and never stored: whoever holds it joins the
  -- agency, as an admin if that is the role.
  token_hash        text not null unique,
  created_by        uuid not null references core.agent(id),
  created_at        timestamptz not null default now(),
  expires_at        timestamptz not null,
  redeemed_at       timestamptz,
  redeemed_agent_id uuid references core.agent(id),
  revoked_at        timestamptz,
  check (expires_at > created_at),
  check ((redeemed_at is null) = (redeemed_agent_id is null)),
  check (redeemed_at is null or revoked_at is null)
);

create index if not exists idx_agent_invite_agency
  on core.agent_invite (agency_id, created_at desc);

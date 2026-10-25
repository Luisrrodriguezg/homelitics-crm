-- ============================================================
-- 014_notifications.sql — the in-app inbox (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: when the AI agent or a TEAM_ADMIN assigns a lead to an agent or
-- books a visit on their calendar, that agent gets a message in the CRM,
-- like a mail inbox: one row per message per recipient, read or unread.
-- Admins see every message of their agency (the monitor) and filter it.
-- No email: the CRM never sends anything outside itself.
-- docs/DECISIONS.md §23.
--
-- No names, addresses or message text are stored: the API joins them in
-- when the inbox is read. A copy here would survive the one-UPDATE
-- right-to-erasure on pii.person (CLAUDE.md rule 1).
--
-- Tracing a message to its outbox event: both rows are written in the
-- same transaction, so created_at = domain_event.occurred_at (now() is
-- the transaction's start time) and lead_id / appointment_id is the
-- event's aggregate_id. No event_id column, so no extra round trip.
-- ============================================================

create table if not exists core.notification (
  id                 bigint generated always as identity primary key,
  agency_id          uuid not null references core.agency(id),
  recipient_agent_id uuid not null references core.agent(id) on delete cascade,
  type               text not null check (type in ('LEAD_ASSIGNED','VISIT_SCHEDULED')),
  lead_id            uuid not null references core.lead(id) on delete cascade,
  -- Set iff type = 'VISIT_SCHEDULED'.
  appointment_id     uuid references core.appointment(id) on delete cascade,
  -- Who caused it: an AI_AGENT row or a TEAM_ADMIN. Never the recipient.
  actor_agent_id     uuid references core.agent(id) on delete set null,
  -- Facts as they were when the message was created, e.g. {"via": "REASSIGNED",
  -- "from_agent_id": ...} or {"scheduled_at": ..., "status": ...}. Ids and
  -- timestamps only, never PII.
  data               jsonb not null default '{}',
  created_at         timestamptz not null default now(),
  read_at            timestamptz,
  check ((type = 'VISIT_SCHEDULED') = (appointment_id is not null))
);

-- The agent's inbox: newest 20 of one recipient.
create index if not exists idx_notification_recipient
  on core.notification (recipient_agent_id, id desc);

-- The unread badge.
create index if not exists idx_notification_unread
  on core.notification (recipient_agent_id)
  where read_at is null;

-- The admin monitor: newest 20 of one agency.
create index if not exists idx_notification_agency
  on core.notification (agency_id, id desc);

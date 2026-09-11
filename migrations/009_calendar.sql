-- ============================================================
-- 009_calendar.sql — the visit calendar: booking rules, .ics export,
-- Google Calendar import, client feedback from the bot (additive)
--
-- Nothing here drops or rewrites data. Safe against a populated
-- database; safe to re-run (idempotent throughout).
--
-- Why: core.appointment already *is* the calendar — one row per visit.
-- What was missing is the rules around it and the ways out of it.
-- docs/DECISIONS.md §19. Checked against the live data on 2026-09-11
-- before writing: 0 leads with two open visits, 0 duplicate feedback
-- per side, so both UNIQUE indexes build and no row changes.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Who booked the visit — a human or an AI agent. Nullable: the
--    seeded history predates it. Also what "booked by the bot" reads.
-- ------------------------------------------------------------
alter table core.appointment
  add column if not exists created_by uuid references core.agent(id);

-- ------------------------------------------------------------
-- 2. At most one open visit per lead. A bot that retries a POST after
--    a timeout lands on this instead of double-booking — the same
--    pattern as lead dedup (DECISIONS §4). The status list mirrors
--    services/appointment._BLOCKING; keep the two in step.
-- ------------------------------------------------------------
create unique index if not exists uq_appointment_open_per_lead
  on core.appointment (lead_id)
  where status in ('PENDING_CONFIRMATION','CONFIRMED','RESCHEDULED');

-- ------------------------------------------------------------
-- 3. One feedback per side (AGENT, CLIENT) per visit, so a retried
--    POST returns the first row instead of adding a second.
-- ------------------------------------------------------------
create unique index if not exists uq_visit_feedback_side
  on core.visit_feedback (appointment_id, submitted_by);

-- ------------------------------------------------------------
-- 4. Export. Calendar apps fetch a feed with no Authorization header,
--    so the secret travels in the URL; rotating it is a new uuid.
--    The volatile default gives every existing row its own value.
-- ------------------------------------------------------------
alter table core.agent
  add column if not exists calendar_token uuid not null default gen_random_uuid();

-- ------------------------------------------------------------
-- 5. Import. One external calendar per agent (Google's "secret address
--    in iCal format"); its busy blocks become time off, so /slots and
--    the booking check need no new logic. The URL is itself a
--    credential: the API masks it and never echoes it back whole.
-- ------------------------------------------------------------
create table if not exists core.agent_external_calendar (
  agent_id       uuid primary key references core.agent(id) on delete cascade,
  ics_url        text not null,
  last_synced_at timestamptz,
  last_status    text check (last_status in ('OK','ERROR')),
  last_error     text,
  created_at     timestamptz not null default now()
);

-- Imported blocks carry their source, so a resync never touches time
-- off an agent typed in by hand. external_uid is the event UID plus the
-- occurrence start: a weekly RRULE is many rows with one UID.
alter table core.agent_time_off
  add column if not exists source text not null default 'MANUAL'
    check (source in ('MANUAL','ICS')),
  add column if not exists external_uid text;

create unique index if not exists uq_agent_time_off_ics
  on core.agent_time_off (agent_id, external_uid)
  where source = 'ICS';

-- ------------------------------------------------------------
-- 6. The bot may record the client's post-visit feedback (the service
--    only lets it write submitted_by = 'CLIENT'). Same shape as 008:
--    new accounts get it by default, existing ones are granted it here.
--    Moving and cancelling a visit need no grant — PATCH /appointments
--    now sits on visits:request, which the bot already holds.
-- ------------------------------------------------------------
alter table core.service_account alter column scopes
  set default '{leads:create,leads:transition,interactions:write,tasks:write,visits:request,clients:create,visits:feedback}';

update core.service_account
   set scopes     = array_append(scopes, 'visits:feedback'),
       updated_at = now()
 where not ('visits:feedback' = any(scopes));

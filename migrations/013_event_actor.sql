-- ============================================================
-- 013_event_actor.sql — every domain event records who caused it
-- (additive; one backfill UPDATE on events.domain_event only)
--
-- Safe against a populated database; safe to re-run (idempotent
-- throughout — the backfill only touches rows still NULL).
--
-- Why: the outbox said *what* happened, never *who*. Auditing the AI
-- agents ("did the Telegram bot create this lead, book this visit?")
-- and the notifier's "X did Y" lines both need the actor on the event,
-- not a join back to whichever core table happened to record it.
-- docs/DECISIONS.md §22.
-- ============================================================

-- The core.agent row that acted: a human, or an AI_AGENT row (join
-- core.agent to tell which). NULL = the system — pg_cron's sweep, or an
-- event from before this migration that the backfill could not match.
--
-- No FK, like agency_id and aggregate_id beside it: the outbox is a log,
-- and the seeders' `truncate core.agent ... cascade` must not wipe it.
alter table events.domain_event
  add column if not exists actor_agent_id uuid;

-- "Everything this actor did, newest first" — the bot audit.
create index if not exists idx_domain_event_actor
  on events.domain_event (actor_agent_id, occurred_at)
  where actor_agent_id is not null;

-- ------------------------------------------------------------
-- Backfill what can be recovered exactly.
-- ------------------------------------------------------------

-- appointment.*: services/appointment._record already put the actor in
-- the payload.
update events.domain_event
   set actor_agent_id = (payload->>'actor_id')::uuid
 where actor_agent_id is null
   and aggregate_type = 'appointment'
   and payload ? 'actor_id';

-- lead.created / lead.stage_changed: the event and its transition were
-- written in one transaction, so occurred_at and changed_at are the same
-- now(). The opening transition is the one with from_stage NULL.
update events.domain_event e
   set actor_agent_id = t.changed_by
  from core.lead_stage_transition t
 where e.actor_agent_id is null
   and t.changed_by is not null
   and e.aggregate_type = 'lead'
   and t.lead_id = e.aggregate_id
   and t.changed_at = e.occurred_at
   and (   (e.event_type = 'lead.created' and t.from_stage is null)
        or (e.event_type = 'lead.stage_changed'
            and t.to_stage = e.payload->>'to'
            and t.from_stage is not distinct from e.payload->>'from'));

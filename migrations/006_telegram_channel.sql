-- ============================================================
-- 006_telegram_channel.sql — the bot channel is TELEGRAM, not WHATSAPP
--
-- Nothing here drops or rewrites structure; it relabels rows and swaps
-- two CHECK constraints. Safe against a populated database; safe to
-- re-run (idempotent throughout).
--
-- Why: inbound leads arrive through a Telegram bot. The value set was
-- authored as WHATSAPP | IN_APP | CALL, so the API rejected TELEGRAM
-- with a 422 and the seeded data labelled bot leads as WhatsApp. The
-- value is *renamed*, not added: keeping WHATSAPP would let the API
-- accept a channel nothing produces. docs/DECISIONS.md §16.
-- ============================================================

-- ------------------------------------------------------------
-- Per table, in one transaction: drop the old CHECK, relabel the rows,
-- add the new CHECK. The order matters — the old constraint rejects
-- TELEGRAM and the new one rejects WHATSAPP, so neither may be in
-- force while the UPDATE runs. The guard (new CHECK already present)
-- makes a re-run a no-op. Constraint names are the ones Postgres
-- generated for the inline CHECKs in 001, confirmed on the live DB.
-- lead.updated_at is left alone: this is a relabel, not a funnel event.
-- ------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'lead_source_channel_check'
      and conrelid = 'core.lead'::regclass
      and pg_get_constraintdef(oid) like '%TELEGRAM%'
  ) then
    alter table core.lead drop constraint if exists lead_source_channel_check;
    update core.lead set source_channel = 'TELEGRAM' where source_channel = 'WHATSAPP';
    alter table core.lead add constraint lead_source_channel_check
      check (source_channel in ('TELEGRAM','IN_APP','CALL'));
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'interaction_channel_check'
      and conrelid = 'core.interaction'::regclass
      and pg_get_constraintdef(oid) like '%TELEGRAM%'
  ) then
    alter table core.interaction drop constraint if exists interaction_channel_check;
    update core.interaction set channel = 'TELEGRAM' where channel = 'WHATSAPP';
    alter table core.interaction add constraint interaction_channel_check
      check (channel in ('TELEGRAM','IN_APP','CALL'));
  end if;
end $$;

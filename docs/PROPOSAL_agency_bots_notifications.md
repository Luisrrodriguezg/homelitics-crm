# Proposal — agency bots, the notifier, and what's left

**Status:** proposal, 2026-10-07. The live, commentable version for the team is
the shared Claude Doc "Homelitics — agency bots & notifier proposal"
(https://claude.ai/code/artifact/7ba847d9-68b0-4b7c-8501-260f44d18dec). Decision
status is tracked there. This file is the in-repo snapshot.
CRM = this repo (`homelitics-crm`). Bot = `Gasvi281/ProyectoSistema2026-2`.

**Principle.** The CRM is the system of record and owns the rules. Bots talk to
clients. The notifier talks to agents. Bots and the notifier are API clients:
they *consult* the CRM, and the CRM never sends a notification itself.

---

## 1. Decisions so far

| # | Decision | Status |
|---|---|---|
| D1 | **One bot per agency** (not per agent). The bot reads the agency's listings (`GET /listings`) to recommend properties, and listings are agency-scoped. | Agreed |
| D2 | Each agency's bot is **configurable by the agency admin only** (persona, templates, rules). | Agreed |
| D3 | Client messaging (HU-12 templates, first touch, re-engagement) **belongs to the bots**, not the CRM. | Agreed |
| D4 | Notifications move **out of the CRM** into a separate notifier that consults the CRM. | Agreed |
| D5 | Add "time to first bot response" next to the human metric. The existing metric is not redefined. | Agreed |
| D6 | Notifier tool and channels (§4.2, §4.4). | To discuss |
| D7 | Bots may write to clients first (reminders, re-engagement). This reverses DECISIONS §19. | To discuss (§6 Q1) |

---

## 2. Spec for a later session — agencies and agents (NOT now)

- 6 agencies (keep), **5–20 agents per agency** (varied, e.g. 5/8/10/12/15/20), plus
  the `AI_AGENT` row per agency.
- **Redistribute the existing leads** to those agents. The rule to keep: *the
  listing's agent owns the lead*, so redistribute listings and the leads follow,
  always inside the same agency.
- Open question for that session: **re-seed** (`seed.py` change) or **data
  migration on the live DB** (add agents, move listings and leads, write
  `assignment_audit`)?
  - Re-seed wipes agent logins, availability rules and AI-agent provisioning,
    and `docs/ground_truth.md` must be re-measured.
  - Migrating keeps history, but scrambles the slow-responder cohort that the
    ground truth depends on.
- Prerequisites: confirm migration **011 is applied** to `homelitics` (it merged
  2026-09-24 and is not yet recorded as applied). Then provision **one service
  account per agency** instead of the shared `ai-agent`. That needs no schema
  change, only a loop in `scripts/provision_ai_agent.sql`.

---

## 3. The agency bot

### 3.1 Where config lives

In the CRM: one migration (012), `core.agency_settings (agency_id pk, bot jsonb,
notify jsonb, updated_at, updated_by)`, validated by Pydantic models.

- `GET /agency/settings` serves the admin and the agency's bot/notifier service accounts.
- `PUT /agency/settings/bot` and `PUT /agency/settings/notify` are TEAM_ADMIN only.

Why the CRM: the admin UI already talks to it, `TeamAdmin` already enforces
"admin only", and the bot already authenticates to it per agency.

**Telegram bot tokens never go in the CRM.** They stay in the bot service's secrets, keyed by agency.

### 3.2 Bot parameters (suggested)

| Group | Parameter | Type | Default | Note |
|---|---|---|---|---|
| Identity | `bot_name` | text | "Homi" | |
| | `tone` | formal \| friendly | friendly | |
| | `language` | text | es-CO | |
| | `use_emoji` | bool | true (light) | |
| Hours | `business_hours` | weekly ranges | Mon–Fri 08–18, Sat 09–13 | `America/Bogota`, fixed |
| | `out_of_hours` | keep_helping \| collect_and_wait | keep_helping | The bot can still search and book |
| Recommendations | `max_results` | int | 3 | Per reply |
| | `price_tolerance_pct` | int | 15 | ± around the client's budget |
| | `operations_offered` | [SALE, RENT] | both | |
| | `cities` | list | agency's cities | |
| | `show_price` | bool | true | `min_acceptable_price` is never exposed (the API doesn't return it) |
| | `exclude_already_contacted` | bool | true | Skip listings the client already has a lead on |
| Booking | `offer_slots_count` | int | 3 | Taken from `/agents/{id}/slots` |
| | `slot_lookahead_days` | int | 7 | |
| | `instant_booking` | bool | false | true needs scope `visits:manage` |
| | min notice | — | 120 min | Global `VISIT_MIN_NOTICE_MINUTES`, read-only |
| Handoff | `handoff_triggers` | list | asks_for_human, price_negotiation, complaint, 3 failed turns | |
| Proactive (pending §6) | `visit_reminder_hours_before` | int | 24 | |
| | `post_visit_feedback_after_hours` | int | 2 | |
| | `re_engagement_after_days` | int | 5 | |
| | `max_unanswered_proactive` | int | 1 | Never send more than one unanswered message |
| | `quiet_hours` | range | 20:00–08:00 | |
| Data | `privacy_notice_url` | url | — | Personal data notice, Colombia Ley 1581/2012 |
| | `require_name_before_lead` | bool | true | |

**Fixed guardrails (not editable):**
- Never reveal the minimum acceptable price.
- Never promise discounts.
- No legal or credit advice.
- Never mention other clients.
- **Every message sent or received is logged to the CRM timeline**
  (`POST /leads/{id}/interactions`), otherwise the funnel metrics break.

### 3.3 Templates (HU-12 → bot config)

Variables: `{client_name} {bot_name} {agency_name} {agent_name}
{listing_address} {neighborhood} {property_type} {bedrooms} {area_m2} {price}
{operation} {visit_date} {visit_time} {slots} {calendar_link} {business_hours}
{privacy_url}`.

| Key | When | Suggested text (es-CO) |
|---|---|---|
| `welcome` | First message | ¡Hola {client_name}! Soy {bot_name}, de {agency_name}. Te ayudo a encontrar inmueble y a agendar visitas. ¿Qué estás buscando? |
| `privacy_notice` | First contact | Usamos tu nombre y contacto solo para atender tu solicitud, según nuestra política: {privacy_url}. ¿Aceptas? |
| `recommendation` | Search results | Encontré estas opciones: {listing_list} ¿Quieres agendar una visita a alguna? (one line each: {property_type} en {neighborhood}, {bedrooms} hab, {area_m2} m² — {price}) |
| `no_results` | Empty search | No tengo algo exacto con eso. Si amplías el presupuesto o la zona, te muestro más. |
| `visit_slots` | Booking | Para {listing_address} tengo estos horarios con {agent_name}: {slots} ¿Cuál te sirve? |
| `visit_requested` | Booked PENDING | Listo, solicité la visita para el {visit_date} a las {visit_time}. {agent_name} la confirma y te aviso. |
| `visit_confirmed` | CONFIRMED | ✅ Visita confirmada: {visit_date}, {visit_time}, en {listing_address}. Agrégala a tu calendario: {calendar_link} |
| `handoff` | Escalation | Te paso con {agent_name}, que te escribe en breve. |
| `out_of_hours` | Outside hours | Nuestro horario es {business_hours}. Sigo ayudándote a buscar y agendar; {agent_name} te responde a primera hora. |
| `visit_reminder` *(proactive)* | 24 h before | Recordatorio: mañana a las {visit_time} visitas {listing_address} con {agent_name}. ¿Sigue en pie? |
| `post_visit_feedback` *(proactive)* | 2 h after | ¿Qué te pareció {listing_address}? Califícalo de 1 a 5 y cuéntanos qué te gustó. |
| `re_engagement` *(proactive)* | N days silent | Hola {client_name}, ¿sigues buscando en {neighborhood}? Entraron opciones nuevas. |

The *proactive* templates require the bot to message first. That reverses
DECISIONS §19, so the team decides (§6).

### 3.4 Admin preview — a small LLM chain in the frontend

The admin edits the config and tries it in a "Try it" chat on the settings page,
as a normal conversation.

- **Where:** bot service, `POST /preview` with `{draft_config, messages[]}`.
  - Auth: the frontend forwards the admin's JWT, and the bot checks it with the
    CRM's `GET /me` (role TEAM_ADMIN, same agency). No new auth system.
- **The chain:**
  1. Build the system prompt from the draft config and templates.
  2. Fetch 3–5 of the agency's real listings once (read-only, `GET /listings`) and inject them as context.
  3. Call a **small, cheap model** — the smallest tier of the Gemini family the bot already uses.
  4. Return the reply.
- **Safety:** no tools that write, nothing stored, history sent by the
  frontend each turn, capped at ~10 turns and short replies, rate-limited per admin.
- **Trade-off:** the preview isn't the real agent graph, so it can drift from
  production behaviour. If that matters, option B runs the real graph with the
  bot's existing `fake` modes for every write (`LEAD_MODE`,
  `APPOINTMENT_BOOKING_MODE`, `CLIENT_RESOLVER_MODE`).

---

## 4. The notifier

### 4.1 How it gets data from the CRM

| Option | How | Verdict |
|---|---|---|
| **A. Pull with a cursor** | New `GET /events?after=<id>`, agency-scoped, scope `events:read`. `events.domain_event.id` is a monotonic identity, so it is a free cursor. | **Recommended.** Every event arrives at least once, and anything missed during downtime is picked up next poll. Dedupe by `event_id`. No migration. |
| B. Push (Supabase DB webhook / `pg_net` on insert) | The CRM calls the notifier | Fire-and-forget: if the notifier is asleep, the event is lost |
| C. Supabase Realtime | Long-lived websocket | Needs an always-on host; no replay after a disconnect |

**Free-tier wake-up.** A poller on a sleeping host never wakes itself. Use
pg_cron (already the only always-on scheduler) plus `pg_net` to POST a "tick" to
the notifier every 5 min. The tick only wakes it; the data still comes from the
cursor, so a lost tick costs nothing. Side effect: it keeps Supabase from
pausing after 7 idle days.

### 4.2 Notifier options (the tool)

| Option | Pros | Cons |
|---|---|---|
| **Python service in the bot repo (`apps/notifier`, LangGraph)** — *recommended* | Same stack and team. Reuses the bot's `SupabaseJwtProvider` and `X-Agency-Id` code. Testable and versioned in git. | One more process to host |
| n8n (self-hosted) — schedule → HTTP `/events` → AI Agent node → Telegram/email | Low-code, quick to demo, built-in AI-agent node | Needs an always-on host and a DB; workflows are hard to test and review; per-agency copies drift |
| Supabase Edge Function on a pg_cron schedule | No extra host | Deno/TypeScript (the team's AI code is Python), execution time limits |
| Novu-style notification platform | Preferences, digests, multi-channel out of the box | Overkill at this size; another vendor |

**One notifier with per-agency settings — not one deployment per agency.**

### 4.3 What "agentic" means here

1. **Rules decide *whether* to notify.** Deterministic: thresholds, event types.
   An LLM never decides if an alert exists.
2. **The LLM decides *how*.** It groups alerts per recipient, picks instant vs
   digest within quiet hours, writes in the agency's tone, and adds context
   through read-only tools (lead, timeline, visits). For example: "Juan wrote 26 h
   ago about the apartment in Laureles. Suggested reply: …".
3. **Delivery** goes through channel adapters, logged with idempotency key
   `(event_id, recipient, channel)`.

The notifier never writes to the CRM and never messages clients directly. A
client-facing message (reminder, re-engagement) is a request to that agency's
bot, if the team allows bots to message first.

### 4.4 Channels

| Audience | Channel | Use |
|---|---|---|
| Agents | **Telegram staff bot** (one "Homelitics Alerts" bot; agents link with a `/start <code>` from their profile) | Instant alerts, daily agenda |
| Agents and admins | Email | Digests, weekly summary (HU-19) |
| Agents | In-app (frontend) | The bell is just a query (`/leads/at-risk`, `/calendar`). No notifier needed. |
| Admins | Agency Telegram group (optional) | Escalations, weekly digest |
| Clients | **Only through the agency's client bot** | Reminders and re-engagement (pending §6) |

WhatsApp is not recommended (cost, Meta business verification).

How an agent is linked: the notifier stores `agent_id → telegram chat`. The CRM
keeps exposing only `agent_id` (`GET /agents` returns no contact details, on purpose).

### 4.5 Notification catalogue (suggested)

| Trigger | Source in CRM | To | Default |
|---|---|---|---|
| New lead | `lead.created` | Owner | Instant |
| Unanswered > threshold (HU-10) | `GET /leads/at-risk?hours=<agency threshold>` (the `hours` override already exists) | Owner; escalate to admin at 2× | 24 h / 48 h |
| Visit needs confirmation | `appointment.booked` (PENDING) | Owner | Instant; nudge after 2 h |
| Visit confirmed / rescheduled / cancelled / no-show | `appointment.*` | Owner | Instant |
| Tomorrow's agenda | `GET /agents/{id}/calendar` | Each agent | Daily 07:00 |
| Visit done, no feedback | `appointment.completed` + feedback list | Owner | +2 h |
| Follow-up task due (HU-11) | **missing:** agency-wide `GET /tasks?due_before=` | Owner | At `due_at`; deep link to the frontend for snooze/done |
| Lead reassigned (HU-08 AC2, cut, now possible) | **missing:** `lead.reassigned` event | New agent | Instant |
| Weekly summary (HU-19) | `/analytics/north-star`, `/analytics/funnel`, … | Admins | Monday 08:00, email |
| Ops: a job stalled | **missing:** `GET /health/jobs` | Dev team | Instant |

### 4.6 Per-agency notify settings (in `agency_settings.notify`)

- `unanswered_threshold_hours` (24)
- `escalate_after_hours` (48)
- `quiet_hours` (21:00–07:00)
- `daily_agenda_time` (07:00)
- `weekly_summary` (on, Mon 08:00)
- `channels_enabled`
- `instant_or_digest` per type
- `max_alerts_per_agent_per_hour` (10)

The notifier keeps its own operational state in its own `notify` schema
(cursor per agency, delivery log, agent channel links), which the CRM never reads.

---

## 5. Background jobs today, and what to improve

| Job | Runs | Does | Problems |
|---|---|---|---|
| `homelitics_sweep` → `core.sweep_inactive_leads(72)` | pg_cron hourly (`005`, predicate redefined in `007`) | Non-terminal leads with no human OUTBOUND MESSAGE/CALL in 72 h and no pending task get: a `follow_up_task` (due +24 h), an auto NOTE on the timeline, and a `lead.went_cold` event. Max 500 per run. | 72 h is hard-coded in the cron command, while the API reads `INACTIVITY_HOURS`: two sources of truth. The auto-note is timeline noise. |
| `homelitics_relay` → `events.relay_domain_events(100)` | pg_cron every 2 min | Marks events `published_at` and raises a first-touch follow-up task for `lead.created` | "Published" is a misnomer: nothing leaves the DB. No retention: events grow forever. |
| `app/jobs.py` APScheduler | Local container only | Same two functions | Correct as is |
| External calendar sync | On demand, 15 min TTL | — | Not a job; fine |

**Events emitted today:** `lead.created`, `lead.stage_changed`,
`appointment.{booked,confirmed,rescheduled,cancelled,completed,no_show,reopened}`,
`lead.went_cold`.

**Improvements:**
1. `GET /events?after=` — the notifier's feed (§4.1).
2. New events: `lead.reassigned`, `interaction.received` (client wrote),
   `task.created`, `feedback.created`.
3. Keep the sweep at 72 h as the CRM's "cold" state; per-agency alert
   thresholds live in the notifier via `/leads/at-risk?hours=`. No CRM change.
4. Drop the sweep's auto-note once the notifier exists. Keep the task and the event.
5. Retention job:
   - Delete published `domain_event` rows older than 90 days.
   - Purge `cron.job_run_details` older than 7 days.
6. Job monitoring: `GET /health/jobs` reports the last successful run per cron
   job; the notifier alerts the dev team if one is stale. **Not inside `/health`**:
   a 503 there fails Render's deploy check.
7. pg_cron + `pg_net` wake-up tick for the notifier (§4.1).

---

## 6. Questions for the team — Telegram communication for what's left

1. **Does the client bot only answer, or may it write first?** If it may, which
   messages: visit reminder, post-visit feedback, re-engagement? Max 1 unanswered
   message? Quiet hours?
2. **Handoff to a human: where does the human talk to the client?**
   - (a) Through the bot: the agent replies from the staff chat and the bot relays
     it, logged as the agent's OUTBOUND MESSAGE.
   - (b) Directly by phone or WhatsApp, and the agent logs it in the CRM.

   Only (a) measures "time to first agent response" automatically.
3. **Agent alerts:** one shared "Homelitics Alerts" Telegram bot, or one per
   agency? An agency Telegram group for admins?
4. **Linking an agent's Telegram:** a `/start <code>` shown in the frontend profile — OK?
5. **Metric:** add "time to first bot response" next to the human one? (We
   don't redefine the existing metric.)
6. **Privacy notice on first contact:** text and link (Ley 1581/2012) — who writes it?
7. **Language and tone:** Spanish only? Tone set per agency by the admin?
8. **Telegram bots:** who creates the 6 agency bots in BotFather and holds the tokens?
9. **Who builds what:**
   - Config UI and preview chat (frontend)
   - `/preview` and the multi-agency webhook (bot repo)
   - Notifier (bot repo `apps/notifier`?)
   - CRM endpoints and migration 012 (this repo)

---

## 7. Appendix — schema and endpoints

### 7.1 Schema today (25 tables, 4 schemas)

| Schema | Tables | Purpose |
|---|---|---|
| `pii` | `person` | Every human once; `telegram_user_id` UNIQUE |
| `core` — people | `agency`, `agent` (role, `auth_user_id`, `calendar_token`), `owner`, `client`, `service_account` | Roles point to `pii.person`; AI agent = service account plus one `AI_AGENT` row per agency |
| `core` — catalogue | `property` (city, neighborhood, area, bedrooms…), `listing` (SALE/RENT, `asking_price`, `min_acceptable_price`, status) | The bot recommends from `listing` |
| `core` — funnel | `lead` (UNIQUE client+listing, `current_stage` cache), `lead_stage`, `lead_stage_transition` (truth, trigger syncs the cache), `lost_reason`, `lead_lost_detail`, `interaction` (timeline), `follow_up_task`, `assignment_audit`, `offer`, `deal`, `objection` | The product |
| `core` — calendar | `appointment` (the calendar), `visit_feedback`, `agent_availability`, `agent_time_off`, `agent_external_calendar` | Visits and slots |
| `events` | `property_view`, `domain_event` (outbox) | Views; the notifier's future feed |
| `analytics` | views `funnel_daily`, `agent_response_time`, `listing_performance`, `lead_outcome`, `stage_conversion` | Dashboards read only these |

**Proposed:**
- `core.agency_settings` (bot + notify jsonb, migration 012).
- Notifier-owned `notify.*` (cursor, delivery log, agent channel link).

### 7.2 Endpoints today (`origin/main`)

| Area | Endpoints |
|---|---|
| Health / me | `GET /health`, `GET /me` |
| Clients | `POST /clients` |
| Leads | `POST /leads`, `GET /leads` (board), `GET /leads/at-risk?hours=`, `GET /leads/{id}`, `GET/POST /leads/{id}/transitions`, `POST /leads/{id}/reassign` (admin), `GET/POST /leads/{id}/interactions`, `GET/POST /leads/{id}/tasks`, `PATCH /leads/{id}/tasks/{task_id}` |
| Visits | `GET/POST /leads/{id}/appointments`, `GET/PATCH /appointments/{id}`, `GET /appointments/{id}/invite.ics`, `GET/POST /appointments/{id}/feedback` |
| Agents | `GET /agents`, `GET/POST /agents/{id}/availability`, `PATCH/DELETE …/availability/{rule_id}`, `GET/POST /agents/{id}/time-off`, `DELETE …/time-off/{off_id}`, `GET /agents/{id}/slots` |
| Calendar | `GET /agents/{id}/calendar`, `GET /calendar`, `GET /agents/{id}/calendar.ics`, `GET /me/calendar-feed`, `POST /me/calendar-feed/rotate`, `GET/PUT/DELETE /me/external-calendar`, `POST /me/external-calendar/sync` |
| Listings | `GET /listings` (filters: status, operation_type, city), `GET /listings/{id}`, `POST /listings/{id}/views` |
| Analytics | `GET /analytics/{funnel-daily, agent-response-time, listing-performance, lost-reasons, north-star}`, `GET /analytics/funnel` (admin, CSV) |

### 7.3 Endpoints left, and who needs them

| Endpoint | For | Why |
|---|---|---|
| `GET /listings` + `price_min/max`, `bedrooms_min`, `neighborhood`, `property_type`, `area_min` | Bot | Recommendations; today only status, operation and city are filterable |
| `GET /listings/{id}/similar?limit=3` | Bot | "Similar to what you viewed" (no consent needed — it is the listing, not the client's history) |
| `GET /agency/settings`, `PUT /agency/settings/{bot,notify}` + migration 012 | Admin UI, bot, notifier | D2 and §4.6 |
| `GET /events?after=&types=&limit=` (scope `events:read`) | Notifier | §4.1 |
| `GET /tasks?status=&due_before=&agent_id=` (agency-wide) | Notifier, frontend | HU-11 reminders; today tasks are per lead only |
| Events `lead.reassigned`, `interaction.received`, `task.created`, `feedback.created` | Notifier | §4.5 |
| `GET /health/jobs` | Notifier / dev team | Job monitoring (§5.6) |
| `GET /analytics/at-risk-by-agent` (+CSV) | Admin | HU-13 |
| `GET /analytics/listing-interest` | Agent | HU-16: views vs contacts vs visits vs % positive |
| `GET /analytics/demand-segments` | Admin | HU-18: demand by neighborhood, price band, bedrooms vs inventory |
| — | — | HU-19 → notifier, using existing analytics. HU-12 → bot templates. HU-20 → still Won't. |

**Gap to note:** the schema has no listing photos. "Want to see photos?" in the bot needs a
`listing_media` table or external URLs.

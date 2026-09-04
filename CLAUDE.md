# Real Estate CRM — MVP (university project, 2026-2)

## What this is

A lead-management CRM for real estate agencies. The product is **not** a property
catalog — the catalog is supporting cast. The product is the funnel: a client
contacts an agent about a listing, the agent responds, a visit happens, it closes
or it dies. Every North Star metric is measured on that funnel.

Target metrics (from the backlog):
- median time to first agent response
- % of leads with at least one follow-up
- lead → visit conversion rate
- % of leads lost within 48h
- stage-to-stage conversion across the funnel

## Stack

- **Database:** Supabase (managed Postgres). No RLS yet — authorization lives in the API.
- **API:** FastAPI + SQLAlchemy 2.0 (async) + asyncpg + Pydantic v2.
- **No event bus.** Kafka was considered and cut. Notifications are sent
  synchronously in the request that triggers them; inactivity detection and the
  outbox relay are **pg_cron jobs** (`005_cron_jobs.sql`), not event consumers.
  The in-process APScheduler in `app/jobs.py` exists only for the local
  container, which has no pg_cron — never enable it against Supabase.
- **Hosting:** the API is stateless, so it runs on a free scale-to-zero host
  (`render.yaml`, Render). EC2 is the always-on alternative, not a requirement.

### Supabase connection strings (this trips everyone up)

- **Migrations / seeding / psql:** session pooler, port **5432**
- **FastAPI runtime:** transaction pooler, port **6543**, and you MUST pass
  `connect_args={"statement_cache_size": 0}` to `create_async_engine` — transaction
  mode doesn't support prepared statements and asyncpg uses them by default.
- Do not use the "Direct connection" string: IPv6-only on the free tier.
- Free-tier projects pause after 7 days idle. Resume in the dashboard before a demo.

## Repo files

- `schema-2.sql` — full DDL. **Idempotent**: starts with `drop schema ... cascade`,
  so re-running wipes and recreates. Paste into Supabase SQL Editor and Run.
  Click "Run without RLS" on the warning popup. Run `scripts/verify_db.py` first —
  if the live schema has drifted, this file will silently destroy it.
- `migrations/` — `001_schema.sql` (the baseline) then `002`–`005` (additive,
  idempotent; all applied to `homelitics`). `schema-2.sql` is kept equal to 001 + … + 005.
- `render.yaml` — free Render deploy. `scripts/provision_agent_users.py` — one
  Auth login per real agent (Admin API), replaces hand-made users.
- `seed.py` — deterministic synthetic data generator.
  `python seed.py --scale small --seed 42 --now 2026-08-31T00:00:00Z`
- `app/` — the FastAPI service. `README.md` covers setup and the EC2 runbook;
  `docs/DECISIONS.md` records the structural choices and why.

## Schema (21 tables, 4 schemas)

**`pii`** — `person`. Every human lives here exactly once.

**`core`** (19) — `agency`, `agent`, `owner`, `client`, `property`, `listing`,
`lead`, `lead_stage`, `lead_stage_transition`, `lost_reason`, `lead_lost_detail`,
`interaction`, `appointment`, `objection`, `visit_feedback`, `follow_up_task`,
`assignment_audit`, `offer`, `deal`

**`events`** — `property_view` (append-only, one row per listing page view)

**`analytics`** — plain views only: `funnel_daily`, `agent_response_time`,
`listing_performance`, plus `lead_outcome` and `stage_conversion` (added in
`002_fixes.sql` for the North Star metrics). Dashboards read from here
exclusively, never from `core` — which is why **every** one of the five exposes
`agency_id`; two of them did not until `002_fixes.sql`.

### The four rules that generate the design

1. **Person vs. role.** `agent`, `owner`, `client` are thin rows pointing at
   `pii.person`. Right-to-erasure = one UPDATE scrubbing the person row; all
   funnel history survives because facts reference role IDs, not names.
2. **Property vs. listing.** Property is the physical asset. Listing is the
   commercial act (SALE or RENT, price, status). One `listing` table with an
   `operation_type` column — **never** split into separate sale/rent tables.
3. **Transitions are truth, `current_stage` is a cache.** The app inserts into
   `lead_stage_transition`; a trigger (`core.sync_lead_stage`) updates
   `lead.current_stage`. This is the only logic in the schema and it is
   load-bearing — without it every funnel metric is unmeasurable.
   The trigger is **guarded** (`002_fixes.sql`): it only writes when the new row
   is the newest by `changed_at`. Before that it wrote in *insert* order, so a
   backdated transition corrupted the cache. Out-of-order transitions are still
   logged; only the cache update is skipped.
4. **Nothing moves between tables by lifecycle.** A won lead stays in `lead` with
   `current_stage='WON'` and gains a `deal` row. A lost lead gains a
   `lead_lost_detail` row. Never a separate "closed leads" table.

### Enforced constraints worth knowing

- `lead` has `UNIQUE (client_id, listing_id)` — this **is** the dedup requirement
  (HU-01 CA3). On insert conflict, return the existing lead thread rather than
  creating a duplicate. Don't reimplement dedup in Python; it races.
- `listing` has `CHECK (min_acceptable_price <= asking_price)`.
- Money is `numeric(15,2)` everywhere, never float. Timestamps are always `timestamptz`.
- Value sets are `text` + `CHECK`, not Postgres enums — deliberately, so they're
  editable with a plain ALTER.

## Deliberate scope cuts (do not "fix" these)

| Cut | Consequence |
|---|---|
| Appointment exclusion constraint | Double-booking prevention is FastAPI's job: query `idx_appointment_agent` for overlaps before insert. Removed because `EXCLUDE USING gist` with an inline `tstzrange` isn't IMMUTABLE and won't create. |
| `consent` table | HU-20 is Won't-this-sprint. Synthetic data ⇒ no data subjects ⇒ no consent obligation during development. |
| Kafka / event bus | Sync calls + scheduled jobs. `events.domain_event` (`004`) is a transactional outbox: `services/events.emit` writes in the caller's transaction, `events.relay_domain_events()` (`005`, pg_cron every 30 s) publishes. The event *data model* survives; only the broker is gone. |
| Always-on host | Jobs run in pg_cron (`005`), so the container may sleep. Free Render deploy via `render.yaml`. `ENABLE_SCHEDULER` is local-only. `docs/DECISIONS.md` §14. |
| Materialized views | Plain views. Always fresh, no refresh job. Backed by measurement — see `docs/DECISIONS.md` §13 (`scripts/measure_views.py`), p95 ≤ 133 ms. Revisit only if one crosses ~500 ms. |
| `updated_at` triggers | The API sets `updated_at` on write. |

**Reinstated (were cuts, now built):**
- **Agent availability** — `003_availability.sql` adds `core.agent_availability`
  (weekly rules) + `core.agent_time_off`. Slot maths is in the API
  (`services/availability.compute_slots`, `America/Bogota`). HU-02 is still
  *request → confirm*; `request_visit` enforces availability only when
  `ENFORCE_AVAILABILITY=true` (default false). See `docs/DECISIONS.md` §11.

Also deferred, additive if needed: `listing_price_history`, `message_template`,
`notification`, `search_event`.

## The seeder

Two phases. `generate()` builds tuples in memory (no DB), `load()` pushes them —
so the generator is testable without a connection.

`--scale small` produces: 18 agents, 250 properties/listings, 927 clients,
1,789 leads, 4,839 transitions, 3,352 interactions, 579 appointments, 111 deals,
76,801 property views. Generation ~1.2s; load 60–120s (network-bound; views go
via `COPY`, everything else batched at 1,000 rows).

### Injected ground truth — this is the point

The simulation deliberately encodes causal patterns so analytics work can be
*validated* rather than assumed. Verified at seed 42:

| Pattern | Expected signal |
|---|---|
| 7/18 agents are slow responders | 30.5h vs 2.0h median first response |
| Slowness causes worse outcomes | 19.3% vs 29.4% lead→visit conversion |
| 27 listings priced +25% over fair value | 493 vs 285 avg views, but 2.1% vs 6.7% win rate |
| ~12% abandoned leads | LOST with `NO_RESPONSE`, zero OUTBOUND interactions |
| ~8% missing emails, 3% duplicate clients with reformatted phones | data-quality test fixtures |

The script prints the exact slow-agent UUIDs at the end — save that block as
`ground_truth.md`. Dashboard correctness tests assert against it.

### Cleaned (HT-03) — do not reintroduce

- the `if False` cruft in `pairs_seen.add(...)` is gone
- the objection update uses `execute_values`, not one round trip per row
- reassignment now updates `lead.agent_id` **and** writes `assignment_audit`, and
  keeps the target inside the same agency
- `appointment.status` is derived in a post-pass (COMPLETED only if the lead
  actually reached VISITED) instead of being hard-coded COMPLETED
- `--now` pins the simulation clock. The generator is translation-invariant, so
  this changes no row counts — it shifts every timestamp as a block, which is
  what makes runs on different days comparable.

## HT-03 — built

FastAPI service in `app/`: routers → services → SQLAlchemy (no repository layer,
no Alembic — see `docs/DECISIONS.md` §2 and §8). Async engine on the 6543 pooler
with `statement_cache_size=0`. Supabase JWT auth via JWKS or legacy HS256,
resolving `sub` → `core.agent.auth_user_id`.

**Authorization is service-layer filtering, not RLS** — forced by the transaction
pooler, which multiplexes sessions and so makes per-request
`SET LOCAL request.jwt.claims` leak across requests. Every service call takes an
`agency_id`. Reasoning in `docs/DECISIONS.md` §1. This is the decision most worth
understanding before changing anything: a service function that forgets its
filter leaks across tenants and nothing else catches it.

Endpoints cover all five priorities: create-or-return lead (via the UNIQUE guard,
201/200), lead board + transitions, interaction timeline, visit request →
confirm → feedback, `/leads/at-risk` plus an hourly pg_cron sweep (`005`), and
analytics reading `analytics.*` only. Plus (Phase 4) `/agents/{id}/availability`,
`.../time-off`, `.../slots`, and the `events.domain_event` outbox with a 30s
relay (`jobs.relay_events`, `on_lead_created` → first-touch follow-up).

`docs/API_GUIDE.md` is the full consume-the-API guide (auth, every endpoint with
examples, the event model, an end-to-end walkthrough). `docs/AC_COVERAGE.md` is
the AC → implementation → verification matrix.

### Local one-command dev

`docker compose --profile local up --build` — throwaway `postgres:17-alpine`,
`migrations/*.sql` auto-applied on first boot (001→004), one-shot `seed`
(`--scale small --seed 42`), API with `DEV_AUTH_BYPASS=true` (identity from
`X-Dev-Agent-Id`; the app refuses to start with the bypass on against a
non-local DB). No `.env`, no Supabase.

### Database state

**Populated** by `seed.py --scale medium --seed 42 --months 12 --now 2026-08-31`,
loaded 2026-09-01 (it replaced the earlier `scripts/seed_sql.sql` load). 6 agencies,
48 agents, 1,200 listings, 9,402 leads, 371,567 property views; all integrity
checks zero. Measured figures and the cohorts are in `docs/ground_truth.md`.
Both seeders TRUNCATE first — re-seeding wipes auth bindings and availability rules.

On 2026-09-03 the live data was cleaned (pytest debris purged, 3 tiny repairs,
default Mon–Fri availability for the 48 agents) — the exact SQL is in the
session's `phase0_cleanup.sql`; `scripts/purge_test_rows.sql` is the reusable part.
`tests/conftest.py` now refuses a non-local `DATABASE_URL` so debris cannot recur.

Migrations `001`–`005` are applied to the live DB; `scripts/verify_db.py` is green.
`.env` is filled and working in this worktree.

### Outstanding

- **Logins:** `python scripts/provision_agent_users.py` (needs
  `SUPABASE_SERVICE_ROLE_KEY` + `DEMO_AGENT_PASSWORD` in `.env`). Until then every
  authenticated request returns 403; `DEV_AUTH_BYPASS` sidesteps this locally.
- **Hosting:** create the Render Blueprint from `render.yaml`, set the four
  secrets, run `scripts/smoke.sh` against the public URL. README "Deploy for free".

## Working style

Be frank and direct. Prefer the simple implementation; if something needs a
Postgres feature that's fighting back, drop it and handle it in the app. Push
back on over-engineering. Text explanations over diagrams unless asked.

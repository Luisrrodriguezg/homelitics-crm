# AC coverage — HT-02 (generator) + HT-03 (backend)

Every acceptance criterion → where it is satisfied → how it is verified. AC
wording is paraphrased from the backlog; the mapping is what matters for review.

Verification commands, from the repo root with `.env` present:

- `python scripts/verify_db.py` — structural + ground-truth checks (21 on Supabase, 20 on the local container which has no pg_cron)
- `pytest` — full suite against the live DB (`tests/test_generator.py` needs no DB)
- `pytest tests/test_generator.py` — generator invariants, no connection
- `python scripts/measure_views.py` — analytics view p95 (DECISIONS §13)
- `docker compose --profile local up --build` — one-command local stack

---

## Story 1 — HT-02: deterministic synthetic data generator

| AC | Requirement | Satisfied by | Verified by |
|---|---|---|---|
| AC1 | Reproducible: same `--seed` ⇒ identical output; bulk load via `COPY` | `seed.py` — seeded `uid()` RNG (not `uuid4`), `Faker.seed`; every table loaded through `copy_expert` in FK order | `tests/test_generator.py::test_same_seed_is_byte_identical`; `seed.py --dry-run` ×2 |
| AC2 | Real geography; rent priced at ~0.4–0.6% of sale per m² | `geo_medellin.py` (39 barrios / 10 municipalities, 5 price tiers, COP/m²); `rent = fair × U(0.004,0.006)` per listing | `tests/test_generator.py::test_rent_is_a_small_fraction_of_sale_per_m2`; read-only SQL on loaded data |
| AC3 | Temporal realism: weekday/seasonal view seasonality; leads *derived from* views via a conditioned funnel | daily Poisson `λ = base × weekly[dow] × monthly[month] × age_decay × price_effect`; views → contacts (logistic) → leads → Markov walk with an explicit advance logit (`is_slow`, price ratio, attractiveness) | `tests/test_generator.py::test_weekend_views_exceed_weekday_views`; `test_now_shifts_the_simulation_as_a_block`; `verify_db.py` ground-truth block |
| AC4 | Injected, documented ground truth for dashboard validation | `seed.py` injects slow responders, overpriced listings, abandoned leads, best-converting segment; `docs/ground_truth.md` records the cohorts and expected signals | `scripts/verify_db.py` "GROUND TRUTH (seed 42)" — 4/4 patterns recovered from `analytics.*`. **Note:** `docs/ground_truth.yaml` + `tests/test_ground_truth.py` (Phase 2) are deliberately deferred |
| AC5 | Data-quality noise fixtures; one-command local dev | 8% missing emails, 3% duplicate clients w/ reformatted phones, ~20% abandoned leads; `docker compose --profile local` | `verify_db.py` data-quality checks; `docker compose --profile local up --build` → `/health` 200, seeded |

---

## Story 2 — HT-03: FastAPI backend

| AC | Requirement | Satisfied by | Verified by |
|---|---|---|---|
| AC1 | Migrations as versioned artifacts | `migrations/001..010`, each idempotent; `scripts/apply_migrations.py`; `schema-2.sql` = 001+…+010. **Deviation:** no Alembic — `docs/DECISIONS.md` §2 | `scripts/verify_db.py` structure checks |
| AC2 | HU-01: create-or-return lead, race-free dedup | `services/lead.create_or_get_lead` — `INSERT … ON CONFLICT DO NOTHING RETURNING`, 201 new / 200 existing; DB `UNIQUE (client_id, listing_id)` | `tests/test_dedup.py` (incl. 6 concurrent posts) |
| — | HU-01 prerequisite: register the client a lead needs | `POST /clients` → `services/client.create_or_get_client`; with `telegram_user_id`, `ON CONFLICT` on `UNIQUE pii.person(telegram_user_id)` (`008`), 201 new / 200 existing; without it, always a new client (DECISIONS §18) | `tests/test_clients.py` (incl. 6 concurrent first messages); `verify_db.py` `(008)` checks |
| AC3 | HU-02/05/06/07/14: visit request → confirm → feedback; agent availability & slots; funnel transitions; timeline | `routers/appointments.py`, `routers/availability.py` (`/availability`, `/time-off`, `/slots`), `routers/leads.py` transitions + interactions; `services/availability.compute_slots` (`America/Bogota`, reuses `_BLOCKING`). Since `009`: the owner confirms (their own bookings are born `CONFIRMED`), AI agents book only published slots, one open visit per lead (retry = 200), `CONFIRMED`/`COMPLETED` move the lead's stage, closing a lead cancels its visits, time off never covers a booked visit (DECISIONS §19) | `tests/test_transitions.py`, `tests/test_appointment_overlap.py`, `tests/test_availability.py`, `tests/test_visit_rules.py`; `verify_db.py` `(009)` data checks |
| — | HU-02/05: the calendar — for the frontend, the agents' phones, the client, and busy time in (`009`) | `services/calendar.py`: `GET /agents/{id}/calendar` + `GET /calendar` (JSON, FullCalendar-shaped), `GET /me/calendar-feed` → a token-URL `.ics` feed, `GET /appointments/{id}/invite.ics` + `google_calendar_url`; `services/calendar_import.py`: `PUT /me/external-calendar` imports a Google/Outlook/iCloud secret address as `ICS` time off (on-demand sync, SSRF allow-list); client feedback via the bot (`visits:feedback`, `CLIENT` only, one per side) | `tests/test_calendar.py`, `tests/test_calendar_import.py` (fixture feed: TZID, all-day, cancelled, free, RRULE, own-UID echo), `tests/test_tenancy.py`; `scripts/smoke.sh` (feed fetched with no auth) |
| AC4 | Domain events / outbox + a consumer | `events.domain_event` (`004`); `services/events.emit` in the caller's txn; `events.relay_domain_events()` (`005`) run by pg_cron every 2 min — `lead.created` → first-touch follow-up (HU-10); on `supabase_realtime` publication | `tests/test_events.py`; `verify_db.py` (`domain_event` present, RLS on, one grant, both job functions, both `cron.job` rows); `pg_publication_tables` |
| AC5 | Happy-path tests + one-command local dev | `tests/*` (20 existing + generator/availability/events); `docker compose --profile local` (postgres + migrations + seed + API w/ `DEV_AUTH_BYPASS`) | `pytest`; `docker compose --profile local up --build` |
| AC6 | Analytics performance | Plain `analytics.*` views, all expose `agency_id`. **Deviation:** not materialized — measured, p95 ≤ 133 ms (`docs/DECISIONS.md` §13) | `scripts/measure_views.py` |
| — | HU-09: lost with a reason. AC1 predefined list + free text; AC2 off the active board, still in history; AC3 feeds analytics (`010`) | AC1 `TransitionCreate.lost_reason` + `note` → `lead_lost_detail`; AC2 `GET /leads?active=true` hides WON/LOST, `?stage=LOST` keeps them reachable, and a `Lost: <reason>` STATUS_CHANGE timeline line; AC3 `analytics.lead_outcome.lost_reason` → `GET /analytics/lost-reasons` (DECISIONS §20) | `tests/test_transitions.py` (LOST invariant, `test_active_board_hides_closed_leads`, `test_lost_reason_is_on_the_timeline`), `tests/test_analytics.py` (incl. tenancy); `verify_db.py` `(010)` check |
| — | HU-08: reassign between agents. AC1 TEAM_ADMIN only; AC2 notify the new agent; AC3 audit trail | AC1 `TeamAdmin` dependency; AC3 `services/lead.reassign` updates `lead.agent_id` and writes `assignment_audit` in one transaction. **AC2 not built, by decision** — DECISIONS §20 | `tests/test_tenancy.py` (`test_reassign_requires_team_admin`, `_rejects_target_outside_agency`, `_moves_lead_and_writes_audit` asserts the audit row) |
| — | Multi-tenant isolation (cross-cutting) | every service call takes `agency_id`, joins `core.agent`; RLS not used — `docs/DECISIONS.md` §1 | `tests/test_tenancy.py` (9 cases, incl. every per-visit route and `?client_id=`) |
| — | Inactivity detection (HU-10/at-risk) | `core.sweep_inactive_leads(72)` hourly via pg_cron (`005`; `jobs.sweep_inactive_leads` calls the same function on the local container) + `/leads/at-risk`; emits `lead.went_cold` | `pytest` (at-risk path); `verify_db.py`; `cron.job_run_details` |
| — | Agent logins | `scripts/provision_agent_users.py` — one Auth user per real agent via the Admin API, bound to `core.agent.auth_user_id` (DECISIONS §15) | `--dry-run`, then `GET /me` → 200 with a password-grant token |
| — | Hosting at $0 | `render.yaml` (free web service, Docker, `/health`), jobs in pg_cron so the container may sleep (DECISIONS §14) | `/health` 200 on the public URL; `scripts/smoke.sh` against it |

**Línea 1 (calendar) answers, 2026-09-03:** 1.1 — `GET /agents/{id}/slots` already
subtracts appointments in `PENDING_CONFIRMATION`/`CONFIRMED`/`RESCHEDULED`
(`services/availability.compute_slots` shares `_BLOCKING` with the overlap check;
proven by `tests/test_availability.py::test_a_busy_block_removes_the_overlapping_slot[appointment]`,
not by `verify_db.py`). 1.4 — `ENFORCE_AVAILABILITY` is **false** on every
environment and must stay false until agents publish real rules; default weekly
rules (Mon–Fri 09–12, 14–18) were seeded for the 48 real agents so `/slots` is
demonstrable.

**Línea 1 update, 2026-09-11 (`009`):** 1.2 — the real source of availability is
now the agent's own calendar: `PUT /me/external-calendar` with Google's secret
iCal address turns busy events into time off, and a block created in Google
disappears from `/slots` after the next sync (≤ 15 min, or
`POST /me/external-calendar/sync`) — asserted through `GET /slots` in
`tests/test_calendar_import.py`. 1.1 — booked visits leaving the grid is still
proven at the service level (`tests/test_availability.py`); `scripts/smoke.sh`
only checks `/slots` answers. 1.3 (reminders) — not
built: the bot is reactive by decision, so nothing would deliver them
(DECISIONS §19). 1.4 — still false for people; AI agents are now held to the
published slots regardless.

---

## Deliberate deviations (each defended in `docs/DECISIONS.md`)

| Named in AC | Chosen instead | Rationale |
|---|---|---|
| Alembic (S2 AC1) | numbered idempotent `.sql` + `apply_migrations.py` | §2 — one target, short files, `schema-2.sql` is the source of truth |
| Materialized views (S2 AC6) | plain views | §13 — measured p95 ≤ 133 ms; matview only when one crosses ~500 ms |
| `ground_truth.yaml` + pattern test (S1 AC4) | `docs/ground_truth.md` + `verify_db.py` ground-truth block | Phase 2 deferred by direction; signals are still verified against `analytics.*` |

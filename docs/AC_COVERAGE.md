# AC coverage — HT-02 (generator) + HT-03 (backend)

Every acceptance criterion → where it is satisfied → how it is verified. AC
wording is paraphrased from the backlog; the mapping is what matters for review.

Verification commands, from the repo root with `.env` present:

- `python scripts/verify_db.py` — 18/18 structural + ground-truth checks
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
| AC1 | Migrations as versioned artifacts | `migrations/001..004`, each idempotent; `scripts/apply_migrations.py`; `schema-2.sql` = 001+002+003+004. **Deviation:** no Alembic — `docs/DECISIONS.md` §2 | `scripts/verify_db.py` structure checks |
| AC2 | HU-01: create-or-return lead, race-free dedup | `services/lead.create_or_get_lead` — `INSERT … ON CONFLICT DO NOTHING RETURNING`, 201 new / 200 existing; DB `UNIQUE (client_id, listing_id)` | `tests/test_dedup.py` (incl. 6 concurrent posts) |
| AC3 | HU-02/05/06/07/14: visit request → confirm → feedback; agent availability & slots; funnel transitions; timeline | `routers/appointments.py`, `routers/availability.py` (`/availability`, `/time-off`, `/slots`), `routers/leads.py` transitions + interactions; `services/availability.compute_slots` (`America/Bogota`, reuses `_BLOCKING`) | `tests/test_transitions.py`, `tests/test_appointment_overlap.py`, `tests/test_availability.py` |
| AC4 | Domain events / outbox + a consumer | `events.domain_event` (`004`); `services/events.emit` in the caller's txn; `jobs.relay_events` 30s relay; `on_lead_created` → first-touch follow-up (HU-10); on `supabase_realtime` publication | `tests/test_events.py`; `verify_db.py` (`domain_event` present, RLS on, one grant); `pg_publication_tables` |
| AC5 | Happy-path tests + one-command local dev | `tests/*` (20 existing + generator/availability/events); `docker compose --profile local` (postgres + migrations + seed + API w/ `DEV_AUTH_BYPASS`) | `pytest`; `docker compose --profile local up --build` |
| AC6 | Analytics performance | Plain `analytics.*` views, all expose `agency_id`. **Deviation:** not materialized — measured, p95 ≤ 133 ms (`docs/DECISIONS.md` §13) | `scripts/measure_views.py` |
| — | Multi-tenant isolation (cross-cutting) | every service call takes `agency_id`, joins `core.agent`; RLS not used — `docs/DECISIONS.md` §1 | `tests/test_tenancy.py` (7 cases) |
| — | Inactivity detection (HU-10/at-risk) | `jobs.sweep_inactive_leads` hourly + `/leads/at-risk`; now also emits `lead.went_cold` | `pytest` (at-risk path); `verify_db.py` |

---

## Deliberate deviations (each defended in `docs/DECISIONS.md`)

| Named in AC | Chosen instead | Rationale |
|---|---|---|
| Alembic (S2 AC1) | numbered idempotent `.sql` + `apply_migrations.py` | §2 — one target, short files, `schema-2.sql` is the source of truth |
| Materialized views (S2 AC6) | plain views | §13 — measured p95 ≤ 133 ms; matview only when one crosses ~500 ms |
| `ground_truth.yaml` + pattern test (S1 AC4) | `docs/ground_truth.md` + `verify_db.py` ground-truth block | Phase 2 deferred by direction; signals are still verified against `analytics.*` |

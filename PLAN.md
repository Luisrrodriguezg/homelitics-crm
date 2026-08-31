# HT-03 — Migrations, seeder fixes, FastAPI service

## Context

This is a continuation of a prior session. `/Users/luisrro/Desktop/Proyecto Home` holds
three files — `CLAUDE.md`, `schema-2.sql`, `seed.py`. The `homelitics` Supabase project
(`msmpbounvqtfobcfyffr`, Postgres 17.6, `ACTIVE_HEALTHY`) has the schema applied and is
empty of data. There is no API, no dependency manifest, no container config, no docs.

The goal is to close HT-03: fix the schema gaps that block per-tenant analytics, load the
synthetic dataset, and stand up a FastAPI service with real Supabase JWT auth — packaged
in a Docker image destined for EC2.

**Session setup.** This session's cwd is `/Users/luisrro/Desktop/AWS`, the wrong project.
The `cp` commands from the previous session never ran, so `Proyecto Home` has no `PLAN.md`
and no `.mcp.json`. First action is `change_directory` to `/Users/luisrro/Desktop/Proyecto Home`,
then copy this plan and `/Users/luisrro/Desktop/AWS/.mcp.json` in. The Supabase connector is
already authenticated in this session — the startup warning was stale, `list_projects`
succeeds.

**Verified live, this session** — the DB matches `schema-2.sql` exactly, so no destructive
re-run is needed and Phase 1 is safe:

- 19 `core` + 1 `pii` + 1 `events` tables; 3 `analytics` views. No drift.
- All data tables empty; `lead_stage`=6, `lost_reason`=6, `objection`=6 populated.
- `core.agent.auth_user_id` does **not** exist.
- `core.sync_lead_stage` is the unconditional version — sets `current_stage` in *insert*
  order, not `changed_at` order.
- Exactly 7 explicit non-PK indexes. **None on `agent(agency_id)`** — the join every
  tenancy check makes — nor on `listing(agent_id)`, `listing(status)`, `property(owner_id)`.
- `anon`/`authenticated` hold no `USAGE` on `pii`/`core`/`events`/`analytics`. PostgREST
  cannot reach this data, which is what makes "authorization lives in the API" safe.

**Decisions locked with the user:** Docker (EC2-bound), Supabase Auth JWT, `agent.auth_user_id`
link, README + OpenAPI. No Alembic — the schema is applied and stable, numbered `.sql` files
are honest and ~20 lines. No repository layer — routers + services is right at this size.
Migrations apply **via the MCP connector** (`apply_migration`, additive only), not by hand
in the SQL Editor. Work proceeds through Phase 3.

## Blocker, stated up front

Phases 1 and 3 need no secrets. **Phase 2 cannot complete**: loading ~76k rows needs
`DATABASE_URL_MIGRATE` (session pooler, port 5432), which only the user can supply. I will
write every seeder fix and stop before running it. Same for end-to-end API verification —
it needs `.env` and two test-user UUIDs. I'll write `.env.example` and say exactly what's
outstanding.

---

## Phase 1 — Migrations

`migrations/001_schema.sql` is a verbatim copy of `schema-2.sql`, kept as the baseline.
`migrations/002_fixes.sql` is additive and applied via `apply_migration`:

- **`core.agent.auth_user_id uuid unique`** + index — the JWT `sub` → agent link.
- **Guard the sync trigger.** Wrap the update in
  `where new.changed_at >= (select max(changed_at) from core.lead_stage_transition ...)`
  so a backdated or concurrent transition can't corrupt the cache. Highest-value fix here.
  The same `create or replace` pins `search_path` to `''`, clearing the Supabase advisor's
  mutable-`search_path` WARN.
- **Add `agency_id` to `analytics.agent_response_time` and `analytics.listing_performance`.**
  Neither exposes it today, so neither can be filtered per tenant, and the API would have
  to read `core` to build dashboards — breaking the rule the schema exists to enforce.
  `agent_response_time` joins `core.agent`; `listing_performance` reaches agency through
  `listing.agent_id → agent`.
- **Two new views** for North Star metrics not derivable from the existing three:
  `analytics.lead_outcome` (per lead: first response time, reached visit, has follow-up,
  lost within 48h) and `analytics.stage_conversion`.
- **Indexes:** `agent(agency_id)`, `listing(agent_id)`, `listing(status)`,
  `property(owner_id)`, `follow_up_task(agent_id, status, due_at)`.
- `REVOKE EXECUTE` on `public.rls_auto_enable()` to silence the anon-callable advisor flag.
  It's an event-trigger function that skips our schemas entirely — cosmetic, noted so the
  advisor comes back clean.

Then patch `schema-2.sql` itself so a wipe-and-recreate keeps all of the above.

`scripts/verify_db.py` (read-only) runs first and confirms the live schema still matches
the file. **If it has drifted I stop and report** — `schema-2.sql` opens with
`drop schema ... cascade` and would wipe the project.

## Phase 2 — Seeder (write the fixes; do not run)

`requirements.txt` + `.venv`. Fixes to [seed.py](seed.py), all confirmed at these lines:

1. **L260-262** — writes `assignment_audit` rows without ever updating `lead.agent_id`,
   and draws the new agent from *every* agency. Update the lead, keep the target in-agency.
2. **L221/223** — `appointment.status` is hard-coded `COMPLETED` even for leads that were
   scheduled and then went LOST before visiting. Derive it in a post-pass from whether a
   `VISITED` transition exists; post-hoc so it consumes no RNG and the seed-42 counts hold.
   Same block: `appointments[-1][0]` works by positional luck — bind a local instead.
3. **L177** — drop the `if False` cruft.
4. **L318-320** — the objection `executemany` is one network round trip per row; switch to
   `execute_values` (already imported at L26).
5. **Add `--now`.** `NOW` is wall-clock at L38, so listing ages and the ~76k view count
   drift every day the seeder runs; CLAUDE.md's seed-42 numbers won't reproduce without
   pinning the date.

Then **stop**. The run — `python seed.py --scale small --seed 42` against port **5432**,
60–120s — and `docs/ground_truth.md` wait on the connection string.

## Phase 3 — The API

```
app/  main.py config.py db.py deps.py auth.py models.py schemas.py
      services/{lead,appointment,analytics}.py  routers/  jobs.py
```

**Engine** — transaction pooler **6543**, `connect_args={"statement_cache_size": 0}`
(non-negotiable: transaction mode has no prepared statements, asyncpg assumes them),
`pool_pre_ping=True`.

**Auth** — `pyjwt[crypto]`, supporting both Supabase key modes: asymmetric via `PyJWKClient`
against `https://msmpbounvqtfobcfyffr.supabase.co/auth/v1/.well-known/jwks.json` (the
default on new projects, needs no secret), or legacy HS256 via `SUPABASE_JWT_SECRET`.
Validate `exp`/`aud`/`iss`, then `sub` → `agent.auth_user_id` → agent row. 401 on missing
or bad token; 403 on a valid token with no matching agent.

**Authorization: service-layer filtering, not RLS.** The API holds one pooled identity and
the transaction pooler multiplexes sessions, so per-request `SET LOCAL request.jwt.claims`
leaks across requests; the analytics views are SECURITY INVOKER and would need policies on
every base table. Every service call takes `agency_id`. Recorded in `docs/DECISIONS.md`.

**Endpoints** — all agency-scoped; `/health` and `/docs` are the only open routes:

- `POST /leads` — `INSERT ... ON CONFLICT (client_id, listing_id) DO NOTHING RETURNING id`,
  SELECT on miss. 201 new / 200 existing. HU-01 CA3 via the DB guard, no Python dedup.
- `GET /leads` (board, filterable), `GET /leads/{id}`, `GET /leads/at-risk`
- `POST /leads/{id}/transitions` — validate the edge, insert, let the trigger update the
  cache. LOST requires a `lost_reason` and writes `lead_lost_detail` in the same
  transaction — the invariant the schema doesn't enforce.
- `POST /leads/{id}/reassign` — updates `lead.agent_id` **and** writes `assignment_audit`;
  rejects a target outside the agency.
- `GET`/`POST /leads/{id}/interactions`
- `POST /leads/{id}/appointments` — the `EXCLUDE` constraint was deliberately cut, so
  overlap prevention is ours: in one transaction, `SELECT ... FOR UPDATE` the agent's
  overlapping rows, 409 on conflict. The row lock is what makes it safe under concurrency.
- `PATCH /appointments/{id}` — confirm / reschedule / cancel / complete / no-show
- `POST /appointments/{id}/feedback`
- `GET`/`POST`/`PATCH /leads/{id}/tasks`
- `GET /listings`, `GET /listings/{id}`, `POST /listings/{id}/views`
- `GET /analytics/{funnel-daily,agent-response-time,listing-performance,north-star}` —
  reading `analytics.*` only

**Inactivity job** — APScheduler in the FastAPI lifespan, hourly: non-terminal leads past
the threshold get a `follow_up_task` and a `NOTE` interaction. Gated by `ENABLE_SCHEDULER`
so multiple workers don't duplicate it.

`scripts/bind_agents.py` binds the user's test-user UUIDs to seeded agents (one `TEAM_ADMIN`,
one plain `AGENT`, picking a known slow responder so analytics show the injected pattern).

## Phase 4 — Docker and docs

`Dockerfile` (`python:3.12-slim`, non-root, healthcheck on `/health`), `docker-compose.yml`
with `env_file: .env`, `.dockerignore` excluding `.env`. Fully env-driven, no baked secrets.
`README.md` — architecture, the 5432-vs-6543 trap, setup, migrations, seeding, Docker, EC2
runbook, troubleshooting. OpenAPI carries the endpoint reference via `summary`/`description`/
`response_model`/`responses` on every route — no hand-maintained duplicate.
Plus `docs/DECISIONS.md`, `.env.example`, `requirements.txt`, `.gitignore`.

## Verification

1. `python scripts/verify_db.py` — schema match, trigger consistency, row counts. Re-run
   `get_advisors` and confirm the `search_path` WARN is gone.
2. Confirm the two new views and five new indexes exist via the connector.
3. `pytest` — four tests on what fails silently: concurrent-POST dedup, appointment overlap
   rejection, cross-agency isolation (agent A reading agency B's lead → 404), illegal stage
   transition rejection. These run against the live DB and need `.env`.
4. `uvicorn app.main:app --reload` → `/docs`, authorize with a real Supabase token.
5. `scripts/smoke.sh` — login → `/me` → same lead twice (**201** then **200**, same id) →
   interaction → request visit → overlapping visit (**409**) → confirm → complete →
   feedback → WON → all 4 analytics endpoints.
6. `docker compose up --build`, hit `/health` and one authenticated endpoint.

Steps 3–6 are blocked until the user supplies `.env`. Steps 1–2 run this session.

## What I'll need from the user (don't paste secrets in chat)

| Item | Where | Goes to |
|---|---|---|
| Session pooler URI, port **5432** | Settings → Database → *Session pooler* | `.env` → `DATABASE_URL_MIGRATE` |
| Transaction pooler URI, port **6543** | same page → *Transaction pooler* | `.env` → `DATABASE_URL` |
| `JWT Secret` — only if on legacy HS256 | Settings → API → JWT Keys | `.env` → `SUPABASE_JWT_SECRET` |
| 2 test users | Auth → Users → Add user | the **UUIDs** only |
| anon key | Settings → API (public by design) | chat, for README curl examples |

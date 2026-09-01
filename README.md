# Homelitics API

Lead-management API for real estate agencies. FastAPI + SQLAlchemy 2.0 (async) +
asyncpg on Supabase Postgres.

The product is the **funnel**, not the property catalogue: a client contacts an
agent about a listing, the agent responds, a visit happens, it closes or it dies.
Every North Star metric is measured on that funnel.

---

## The one thing that trips everyone up

Supabase gives you three connection strings. Two of them are used here, for
different things, and they are **not** interchangeable.

| Use | Pooler | Port | Variable | Driver |
|---|---|---|---|---|
| API runtime | Transaction | **6543** | `DATABASE_URL` | `postgresql+asyncpg://` |
| Migrations, seeding, `verify_db.py`, `bind_agents.py` | Session | **5432** | `DATABASE_URL_MIGRATE` | `postgresql://` |

Two rules follow:

* The async engine **must** pass `connect_args={"statement_cache_size": 0}`.
  Transaction-mode pooling hands you a different backend per transaction, so
  asyncpg's cached prepared statements usually are not there — you get
  `prepared statement __asyncpg_stmt_x__ does not exist`, often only under load.
  `app/db.py` does this; do not remove it.
* Do **not** use the "Direct connection" string. It is IPv6-only on the free tier.

Free-tier projects pause after 7 days idle. Resume in the dashboard before a demo.

---

## Layout

```
app/
  main.py        FastAPI app, CORS, lifespan
  config.py      settings (pydantic-settings)
  db.py          async engine — the 6543 trap lives here
  auth.py        Supabase JWT: JWKS (asymmetric) or HS256 (legacy)
  deps.py        token -> core.agent; the tenancy boundary starts here
  models.py      SQLAlchemy 2.0, schema= in __table_args__
  schemas.py     Pydantic v2 + the legal funnel edges
  services/      lead, appointment, listing, analytics
  routers/       one per domain
  jobs.py        hourly inactivity sweep (APScheduler)
migrations/      001_schema.sql (baseline), 002_fixes.sql (additive)
scripts/         verify_db.py, bind_agents.py, smoke.sh
tests/           dedup race, overlap lock, tenancy isolation, funnel edges
docs/            DECISIONS.md, ground_truth.md
```

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill it in
```

`.env` needs both connection strings and `SUPABASE_PROJECT_REF`. Leave
`SUPABASE_JWT_SECRET` **blank** unless the project is on legacy HS256 — modern
projects sign with asymmetric keys and the API fetches the public JWKS, so no
secret is needed.

### 1. Check the database before touching it

```bash
python scripts/verify_db.py
```

Read-only. Run it first, every time. `schema-2.sql` starts with
`drop schema ... cascade`, so if the live schema has drifted you want to know
*before* anything destructive runs. Non-zero exit means stop.

### 2. Migrations

Already applied to `homelitics`. On a fresh project, run `migrations/001_schema.sql`
then `migrations/002_fixes.sql` in the Supabase SQL Editor (click "Run without
RLS" on the warning). `schema-2.sql` is the same thing as one idempotent file.

What `002_fixes.sql` adds, and why, is in [docs/DECISIONS.md](docs/DECISIONS.md) —
the short version: the `auth_user_id` link, a **guard on the stage-sync trigger**
(it previously wrote in insert order, not `changed_at` order), `agency_id` on the
two analytics views that lacked it, two new views for the North Star metrics, and
five missing indexes including `agent(agency_id)`.

### 3. Seed

**The live `homelitics` database is already populated** — ~499,000 rows loaded
2026-08-31 by [`scripts/seed_sql.sql`](scripts/seed_sql.sql). You can skip this
step entirely unless you want to reset it. See
[docs/ground_truth.md](docs/ground_truth.md) for what is in there.

There are two seeders and they are **not** interchangeable:

| | `scripts/seed_sql.sql` | `seed.py` |
|---|---|---|
| needs a connection string | **no** — runs inside Postgres | yes (`DATABASE_URL_MIGRATE`) |
| how to run | paste into the Supabase SQL Editor | `python seed.py ...` |
| size | 13,000 leads / 375k views | 1,789 leads / 77k views |
| reproducible row-for-row | no (uses `random()`) | **yes** (`--seed 42`) |
| currently loaded | **yes** | no |

Both **TRUNCATE every table first.** Running `seed.py` will destroy the
SQL-seeded data and replace it with the smaller, reproducible set.

```bash
python seed.py --scale small --seed 42 --now 2026-08-31T00:00:00Z
```

Uses `DATABASE_URL_MIGRATE` (5432). Takes 60–120s — network-bound, views go via
`COPY`. **It truncates every table first.**

`--now` pins the simulation clock. The generator is translation-invariant, so
this does not change any row counts; it shifts every timestamp as a block, which
is what makes two runs on different days comparable.

Save the printed ground-truth block to `docs/ground_truth.md` — the analytics
tests assert against it. At `--scale small --seed 42` it produces 18 agents,
250 listings, 927 clients, 1,789 leads, 4,839 transitions, 579 appointments,
111 deals and 76,801 property views, with 7 slow-responder agents and 27
overpriced listings planted so analytics work can be *validated* rather than
assumed.

Then re-run `verify_db.py`: with data present it also asserts the trigger held
through the bulk load, dedup is intact, and the injected patterns are visible.

### 4. Bind your Supabase users to agents

Until this runs, every authenticated request gets **403** — the token is valid
but no `core.agent` row points at that user.

```bash
# Supabase → Authentication → Users → Add user (make two), then:
python scripts/bind_agents.py --admin <UUID> --agent <UUID> --dry-run
python scripts/bind_agents.py --admin <UUID> --agent <UUID>
```

It picks a `TEAM_ADMIN` and a plain `AGENT` from the busiest agency, choosing a
known **slow responder** for the latter so the analytics endpoints show the
planted pattern rather than a flat line.

### 5. Run

```bash
uvicorn app.main:app --reload
```

Docs at http://localhost:8000/docs. Click **Authorize** and paste a real access
token.

---

## Getting a token

```bash
curl -s -X POST "https://<PROJECT_REF>.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: <ANON_KEY>" -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"..."}' | jq -r .access_token
```

The anon key is public by design. Then:

```bash
curl -s localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

`/me` is the right first call when debugging auth:

* **401** — the token is missing, expired, or not from this project
* **403** — the token is fine, but no agent is bound (run `bind_agents.py`)

---

## Endpoints

The OpenAPI schema at `/docs` is the reference — every route carries a summary,
description and its error responses. There is no second copy here to fall out of
date. In outline:

* `POST /leads` — create-or-return. **201** new, **200** existing.
* `GET /leads`, `GET /leads/{id}`, `GET /leads/at-risk`
* `POST /leads/{id}/transitions` — LOST requires a `lost_reason`
* `POST /leads/{id}/reassign` — TEAM_ADMIN only
* `GET`/`POST /leads/{id}/interactions`, `.../tasks`, `.../appointments`
* `PATCH /appointments/{id}`, `POST /appointments/{id}/feedback`
* `GET`/`POST`/`PATCH`/`DELETE /agents/{id}/availability` — weekly reachable blocks (HU-05)
* `GET`/`POST`/`DELETE /agents/{id}/time-off`
* `GET /agents/{id}/slots?from=&to=` — free 30-min grid: weekly rules − time off − blocking visits
* `GET /listings`, `GET /listings/{id}`, `POST /listings/{id}/views`
* `GET /analytics/{funnel-daily,agent-response-time,listing-performance,north-star}`

Everything except `/health` and the docs requires a bearer token and is scoped to
the caller's agency.

### Domain events

`services/events.emit` writes an `events.domain_event` row **in the request's
transaction** (`lead.created`, `lead.stage_changed`, `appointment.booked`,
`lead.went_cold`). `jobs.relay_events` runs every `EVENT_RELAY_SECONDS` (gated by
`ENABLE_SCHEDULER`), dispatches unpublished rows to in-process handlers
(`on_lead_created` raises the first-touch follow-up) and stamps `published_at`.
The table is on the `supabase_realtime` publication with RLS + an agency policy —
the only grant `authenticated` holds anywhere in our schemas.

### Extra environment variables

| var | default | meaning |
|---|---|---|
| `EVENT_RELAY_SECONDS` | `30` | outbox relay interval (needs `ENABLE_SCHEDULER=true`) |
| `APP_TIMEZONE` | `America/Bogota` | zone the availability slot maths runs in |
| `ENFORCE_AVAILABILITY` | `false` | when true, `request_visit` rejects an unpublished slot |
| `DEV_AUTH_BYPASS` | `false` | local only — identity from `X-Dev-Agent-Id`; app refuses to start against a non-local DB |

---

## Tests

```bash
pytest
```

Integration tests against a real database — the things they cover (a UNIQUE
race, a row lock, cross-tenant filtering, a trigger, the outbox, slot maths) are
all database behaviour, and a mock would prove nothing. They build and tear down
their own two-agency fixture, so they work on an empty or a seeded database.
Only the tests that need a DB are gated on `DATABASE_URL`; `tests/test_generator.py`
runs without one.

The Supabase pooler is slow from a laptop (~40 s/test). Point pytest at the
local compose Postgres instead:

```bash
docker compose --profile local up -d db
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres \
  SUPABASE_PROJECT_REF=local-dev pytest      # full suite in seconds
```

```bash
API=http://localhost:8000 TOKEN=... CLIENT_ID=... LISTING_ID=... ./scripts/smoke.sh
```

Walks the whole funnel end to end and asserts the interesting statuses: the same
lead twice (201 then 200, same id), an overlapping visit (409), an illegal stage
skip (409), and all four analytics endpoints.

---

## Docker

```bash
docker compose up --build -d
curl localhost:8000/health
docker compose logs -f api
```

`env_file: .env`, nothing baked into the image, `.env` excluded by
`.dockerignore`. Non-root user, healthcheck on `/health` (which round-trips to
Postgres, so a broken database marks the container unhealthy).

### One-command local stack (no Supabase, no `.env`)

```bash
docker compose --profile local up --build
```

Brings up a throwaway `postgres:17-alpine`, applies `migrations/*.sql` on first
boot (001 → 004 in filename order — `004`'s Realtime block no-ops without an
`auth` schema), runs a one-shot seed (`--scale small --seed 42`), then starts the
API on `:8000` with `DEV_AUTH_BYPASS=true`. Send `X-Dev-Agent-Id: <core.agent
uuid>` instead of a bearer token. The API refuses to start if the bypass is on
while `DATABASE_URL` is not local.

### EC2 runbook

1. Launch Amazon Linux 2023, `t3.small` or better. Security group: inbound 22
   from your IP, 80/443 from anywhere. **Do not open 8000 to the world** — put
   TLS in front of it.
2. Install Docker:
   ```bash
   sudo dnf install -y docker
   sudo systemctl enable --now docker
   sudo usermod -aG docker ec2-user   # log out and back in
   ```
3. Copy the project up (`git clone`, or `rsync` excluding `.venv`).
4. Create `.env` **on the instance** — never commit it, never bake it in.
5. `docker compose up -d --build`
6. Verify: `curl localhost:8000/health`
7. TLS. Simplest is Caddy in front, which gets a certificate automatically:
   ```bash
   docker run -d --name caddy --network host -v caddy_data:/data \
     caddy caddy reverse-proxy --from api.example.com --to localhost:8000
   ```

Scaling past one instance: set `ENABLE_SCHEDULER=false` everywhere except one, or
the inactivity sweep raises duplicate tasks. See DECISIONS.md §7.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `prepared statement __asyncpg_stmt_x__ does not exist` | `statement_cache_size: 0` missing, or you are on 5432 instead of 6543 |
| Everything 403s, `/health` fine | No agent bound — run `scripts/bind_agents.py` |
| 401 with a token that works elsewhere | Token is from a different Supabase project; `iss`/`aud` are checked |
| `Network is unreachable` on connect | You used the Direct connection string — IPv6-only on free tier |
| Connection hangs, project looks fine | Free tier paused after 7 days idle; resume in the dashboard |
| `seed.py` is slow | Expected: 60–120s, network-bound |
| Duplicate follow-up tasks | `ENABLE_SCHEDULER=true` on more than one worker |
| `verify_db.py` fails | Live schema drifted. **Do not** run `schema-2.sql` — it wipes. Reconcile first. |

---

## Further reading

* [docs/DECISIONS.md](docs/DECISIONS.md) — RLS vs service-layer filtering, no
  Alembic, the overlap lock, the trigger guard, and the rest.
* [CLAUDE.md](CLAUDE.md) — the data model and the four rules that generate it.

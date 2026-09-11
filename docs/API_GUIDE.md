# Consuming the Homelitics API

A lead-management API for real-estate agencies. **The product is the funnel** — a
client contacts an agent about a listing, the agent responds, a visit happens, it
closes or it dies. The property catalogue is supporting cast. Every metric is
measured on that funnel.

- Interactive reference: **`/docs`** (Swagger UI) and **`/redoc`**
- Machine-readable spec: **`/openapi.json`**
- This guide is the narrative version: how the pieces fit and how to drive them.

---

## 1. Base URLs

| Environment | Base URL | Auth |
|---|---|---|
| Local (`docker compose --profile local up`) | `http://localhost:8000` | `X-Dev-Agent-Id` header (bypass) |
| Supabase-backed deploy | `https://<host>` | `Authorization: Bearer <supabase-jwt>` |

Everything below uses `$BASE` for the base URL and `$AUTH` for whichever auth
header your environment needs.

> **`curl` note:** passing `-d` makes `curl` send `POST`. For a `PATCH` or
> `DELETE` route you must add `-X PATCH` / `-X DELETE`, or the API answers `405`.

---

## 2. Authentication

Every route **except `/health` and the docs** needs an identity. The identity is
resolved to exactly one `core.agent` row, and **that agent's `agency_id` scopes
every query for the rest of the request**. You never send an `agency_id`
yourself — it is derived from who you are.

### 2a. Production — Supabase JWT

```
Authorization: Bearer <access_token>
```

The token's `sub` claim (a Supabase `auth.users` UUID) is matched against
`core.agent.auth_user_id`. That column is `NULL` on freshly seeded agents, so a
valid token for an unbound user returns **403** until the logins are provisioned:
`scripts/provision_agent_users.py` creates one Auth user per real agent
(`<name>@<agency>.homelitics.test`, one shared `DEMO_AGENT_PASSWORD`) and binds
it — see README step 4. Get a token with the Supabase JS client, the Supabase
CLI, or a password-grant call to `${SUPABASE_URL}/auth/v1/token?grant_type=password`.

- **401** — missing, malformed, or expired token
- **403** — valid token, but no `core.agent` is bound to that user

### 2b. Local development — `DEV_AUTH_BYPASS`

The compose `local` profile starts the API with `DEV_AUTH_BYPASS=true`. There is
no token; you send the agent UUID directly:

```
X-Dev-Agent-Id: <core.agent UUID>
```

The app **refuses to start** with the bypass on unless `DATABASE_URL` points at
`localhost` / `db`, so this can never be enabled against a real database.

Get a usable agent id from the seeded data:

```bash
docker compose exec db psql -U postgres -tA -c \
  "select a.id, a.role, ag.name
   from core.agent a join core.agency ag on ag.id = a.agency_id
   where ag.name not like 'pytest-%'
   order by a.role desc limit 10"
```

Pick a `TEAM_ADMIN` for full access (only a `TEAM_ADMIN` can reassign leads).

In **Swagger** under the bypass: click **Authorize**, paste the UUID into the
single `X-Dev-Agent-Id` field, **Authorize** → **Close**. Every "Try it out"
request then carries the header. (Hard-refresh the page if you rebuilt the API,
so Swagger reloads the spec.)

### 2c. Tests

`tests/conftest.py` stubs `get_current_agent` via `dependency_overrides` and
passes the acting agent in an `X-Test-Agent-Id` header — auth is bypassed so the
tests exercise *authorization* (agency scoping), not *authentication*.

### 2d. Service accounts — an AI agent across every agency

A human login maps to one agent in one agency. An AI agent needs all of them,
so it authenticates as a **service account** (`core.service_account`, migration
`007`) and names the agency per request:

```
Authorization: Bearer <access_token>
X-Agency-Id: <core.agency UUID>
```

The token's `sub` matches `core.service_account.auth_user_id`; the header picks
which of that account's per-agency `AI_AGENT` rows to act as. From there the
request is indistinguishable from a human's in that agency — same filters, same
404s — except for the guardrails below. The bot **never owns a lead**: a lead it
creates belongs to the listing's agent, and it cannot be a reassignment target.
`/me` reports `"role": "AI_AGENT"`.

#### Set it up, step by step

Steps 1–5 are done **once**, by whoever owns the Supabase project. Steps 6–8
are what the bot does **every session**. Step 9 is what to do when a call
fails.

**One-time setup**

1. **Deploy the code.** Service-account login lives in the API, so the code
   that added it must be on `main`. Render redeploys by itself on every push
   (`autoDeploy: true` in `render.yaml`). This prints `X-Agency-Id` once the
   new code is serving (the first request after 15 min idle takes ~30–60 s):

   ```bash
   curl -s https://homelitics-api.onrender.com/openapi.json | grep -o X-Agency-Id | head -1
   ```

2. **Apply migration `007` to the database.** Already done on `homelitics`
   (2026-09-10). On a fresh project, paste `migrations/007_ai_agents.sql` into
   the Supabase SQL Editor and Run; `python scripts/verify_db.py` should then
   show the four `(007)` checks as PASS.

3. **Create the bot's login.** Supabase dashboard → **Authentication → Users →
   Add user → Create new user**:
   - email: e.g. `ai-agent@homelitics.test` — a `.test` address, no mail is
     ever sent;
   - password: long and random (`openssl rand -base64 32`);
   - tick **Auto Confirm User**.

   Copy the new user's **UUID** (the `id` column of the users list).

4. **Create the service account.** Open `scripts/provision_ai_agent.sql`, set
   `v_auth` to that UUID (and `v_name` if you want a name other than
   `ai-agent`), paste the whole file into the SQL Editor, Run. It creates the
   `core.service_account` row with the default scopes plus one `AI_AGENT` row
   per agency, and ends by listing every agency with its `agency_id` — the
   values the bot sends as `X-Agency-Id`. Safe to re-run: it only adds what is
   missing.

5. **Give the bot its configuration — and nothing more:**

   | Setting | Value | Where it comes from |
   |---|---|---|
   | `API_BASE` | `https://homelitics-api.onrender.com` | README "Live deployment" |
   | `SUPABASE_URL` | `https://<project-ref>.supabase.co` | Dashboard → Project Settings → API |
   | `SUPABASE_ANON_KEY` | the anon / publishable key | same page — public by design |
   | `BOT_EMAIL`, `BOT_PASSWORD` | from step 3 | a secret store, never the repo |
   | agency ids | the list printed in step 4 | |

   Never the service-role key, never `DATABASE_URL`: the bot only ever talks to
   the API.

**What the bot does**

6. **Get a token.** It lasts one hour:

   ```bash
   curl -s -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
     -H "apikey: $SUPABASE_ANON_KEY" -H 'Content-Type: application/json' \
     -d "{\"email\":\"$BOT_EMAIL\",\"password\":\"$BOT_PASSWORD\"}"
   # -> {"access_token": "...", "refresh_token": "...", "expires_in": 3600, ...}
   ```

   Before it expires, trade the refresh token for a new pair: same URL with
   `grant_type=refresh_token` and body `{"refresh_token": "..."}`. A refresh
   token works once — keep the new one it returns.

7. **Check the identity**, once per agency:

   ```bash
   curl -s $API_BASE/me -H "Authorization: Bearer $TOKEN" -H "X-Agency-Id: $AGENCY_ID"
   # -> {"role": "AI_AGENT", "agency_id": "<AGENCY_ID>", "full_name": "AI Assistant (ai-agent)", ...}
   ```

8. **Work leads.** Every call carries both headers:

   ```bash
   H=(-H "Authorization: Bearer $TOKEN" -H "X-Agency-Id: $AGENCY_ID" -H 'Content-Type: application/json')

   # someone wrote in -> their client id (201 first time; 200 + same id every time after)
   curl -s "${H[@]}" $API_BASE/clients \
     -d '{"telegram_user_id":<message.from.id>,"full_name":"Ana Gómez","phone":"3001234567"}'

   # ...about a listing -> lead (201; 200 if the thread already exists)
   curl -s "${H[@]}" $API_BASE/leads \
     -d '{"client_id":"<uuid>","listing_id":"<uuid>","source_channel":"TELEGRAM","message":"¿Sigue disponible?"}'

   # the bot answers, then books the visit
   curl -s "${H[@]}" $API_BASE/leads/$LEAD/interactions \
     -d '{"direction":"OUTBOUND","channel":"TELEGRAM","body":"¡Hola! Sí, sigue disponible."}'
   curl -s "${H[@]}" $API_BASE/leads/$LEAD/transitions \
     -d '{"to_stage":"VISIT_SCHEDULED","note":"Visita agendada por el asistente"}'
   ```

   Listings are agency-scoped like everything else: a listing from another
   agency is a 404. Call `GET /listings` once per agency id, keep a
   listing → agency map, and send the matching header. The lead is owned by
   the listing's human agent, not the bot. Clients are the exception: they
   belong to no agency, so `POST /clients` returns the same client whichever
   agency header it is sent with. Always send `telegram_user_id`; it is what
   makes the second message from the same person land on the same client.

9. **When a call fails:**

   | Response | Meaning | Fix |
   |---|---|---|
   | **401** | token missing, expired, or from another Supabase project | get a new one (step 6) |
   | **400** | no `X-Agency-Id`, or not a UUID | send the header |
   | **403** "not linked to an agent" | no service account for this login — step 4 not run, wrong UUID, or step 1 not deployed yet | run step 4 with the UUID from step 3; check step 1 |
   | **403** "no AI_AGENT row in that agency" | the database was re-seeded, or the agency is new | re-run step 4 |
   | **403** "deactivated" | the kill switch is on (below) | turn it back on |
   | **403** "lacks the '…' scope" | that write is not granted | grant it (below), if it should be |
   | **404** | the lead/listing belongs to a different agency | use that agency's id |
   | **429** | hourly write budget spent | wait; raise `hourly_write_limit` if the volume is legitimate |

**Rotating the password.** Create a new Auth user (step 3), re-run step 4 with
its UUID — the script re-points the existing `ai-agent` account at it — give the
bot the new credentials, then delete the old user.

**Scopes.** Every write route names a scope; the account's `scopes` array must
contain it or the call is **403**. Reads need no scope. Humans are never scope
checked. Default grant: `leads:create`, `leads:transition`, `interactions:write`,
`tasks:write`, `visits:request`, `clients:create` (added by `008`, which also
granted it to existing accounts). Not granted by default: `leads:close` (moving
a lead to `WON`/`LOST` — on top of `leads:transition`), `visits:manage`
(`PATCH /appointments/{id}`), `visits:feedback`, `availability:write`,
`listings:views`. Reassign stays `TEAM_ADMIN`-only, so a bot can never do it.
Grant a scope with one UPDATE, no deploy:

```sql
update core.service_account
   set scopes = array_append(scopes, 'leads:close'), updated_at = now()
 where name = 'ai-agent';
```

**Budget.** `hourly_write_limit` (default 300) caps transitions + interactions
the account writes in any rolling hour, across all its agencies; past it every
write returns **429** with `Retry-After`. It exists to stop a runaway loop, not
to meter usage. **Kill switch:** `update core.service_account set active = false
where name = 'ai-agent'` — 403 on the bot's next request. `core.agent.active =
false` on one bot row turns it off for that agency only.

**Metrics.** An *agent response* is an `OUTBOUND` `MESSAGE` or `CALL` written
by a human. Anything an `AI_AGENT` writes, and any `NOTE` / `STATUS_CHANGE`
(including the 72h sweep's own auto-note), sits on the timeline but does not
stop the response clock in `analytics.agent_response_time` /
`analytics.lead_outcome`, and does not count as contact for `/leads/at-risk`
or the sweep. A lead the bot is chatting on that no human has answered still
shows as unanswered — which is the point.

---

## 3. Conventions

**Tenancy & 404-not-403.** Anything outside your agency is reported as **404
Not Found**, never 403 — the API will not confirm that a lead/listing/appointment
exists in someone else's agency. A 403 means *you are authenticated but lack the
role* (e.g. reassignment needs `TEAM_ADMIN`).

**Money is a string.** `asking_price`, `amount`, etc. are JSON strings holding a
`numeric(15,2)` (`"650137717.29"`), never floats. Parse with a decimal type.

**Timestamps are UTC ISO-8601** with an explicit offset
(`2026-09-07T16:00:00Z` / `...+00:00`). Send them the same way. Availability slot
maths happens in `America/Bogota` but every timestamp on the wire is UTC.

**Pagination.** List endpoints take `limit` (default 50, max 200) and `offset`
(default 0). Results are ordered newest-activity-first unless noted.

**Errors** are always `{"detail": "<human message>"}`. See §9 for the full table.
Pydantic validation failures (`422`) may instead return FastAPI's structured
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.

**`current_stage` is a cache.** You move a lead by appending a *transition*; a
database trigger updates `lead.current_stage`. Never treat `current_stage` as
writable.

**Dedup is the database's job.** `POST /leads` with a `(client_id, listing_id)`
that already exists returns the existing thread with **200**, not a duplicate.

---

## 4. Quick start

```bash
BASE=http://localhost:8000
AGENT=<paste a TEAM_ADMIN uuid>
AUTH="X-Dev-Agent-Id: $AGENT"          # or: AUTH="Authorization: Bearer $TOKEN"

# who am I?
curl -s -H "$AUTH" $BASE/me | jq

# the funnel board for my agency
curl -s -H "$AUTH" "$BASE/leads?limit=5" | jq

# the five North Star metrics
curl -s -H "$AUTH" $BASE/analytics/north-star | jq
```

---

## 5. Data model in one screen

| Concept | Table | Notes |
|---|---|---|
| A human | `pii.person` | every person exists here once; erasure = scrub this one row |
| Agent / Owner / Client | `core.agent` / `owner` / `client` | thin rows pointing at a person |
| Physical asset | `core.property` | address, area, beds, baths |
| Commercial act | `core.listing` | `operation_type` = `SALE` \| `RENT`, price, status. **One table**, never split |
| The conversation | `core.lead` | `UNIQUE (client_id, listing_id)` — this *is* dedup |
| Stage history | `core.lead_stage_transition` | append-only, the source of truth |
| Timeline | `core.interaction` | `INBOUND` / `OUTBOUND`; first `OUTBOUND` drives response-time |
| Visit | `core.appointment` | request → confirm → complete; feedback after |
| Availability | `core.agent_availability` / `agent_time_off` | weekly rules + ad-hoc time off |
| Events | `events.domain_event` | transactional outbox (see §8) |
| Dashboards | `analytics.*` views | read these, never `core` |

**The funnel:** `INTERESTED → VISIT_SCHEDULED → VISITED → NEGOTIATING → WON`.
Any non-terminal stage may jump to `LOST`. `WON` and `LOST` are terminal.

---

## 6. Endpoint reference

### 6.0 Identity & health

#### `GET /me`
The agent behind the current credential.

```bash
curl -s -H "$AUTH" $BASE/me
```
```json
{
  "id": "4458a885-ab90-49a4-b5a2-40ae5af30553",
  "agency_id": "5ec42e08-29a3-42e9-9d65-a441d58842de",
  "role": "TEAM_ADMIN",
  "active": true,
  "full_name": "Hernando Carrillo",
  "email": null
}
```

#### `GET /health`
Open, no auth. `200` only if a round-trip to Postgres succeeds; `503` otherwise.
Safe as a container healthcheck.

---

### 6.1 Listings & view events

#### `GET /listings`
Catalogue for your agency, newest first.

| query | |
|---|---|
| `status` | `ACTIVE` \| `PAUSED` \| `CLOSED` |
| `operation_type` | `SALE` \| `RENT` |
| `city` | exact match |
| `limit` / `offset` | pagination |

```json
{
  "id": "8d03c91e-4f8d-4238-a88b-78b5b5b453ca",
  "property_id": "300568d2-0de0-41a6-a9ca-97d2764414fd",
  "agent_id": "451b4cf3-6123-4df7-b656-af7229d4beef",
  "operation_type": "SALE",
  "asking_price": "650137717.29",
  "status": "ACTIVE",
  "published_at": "2026-08-01T20:52:40.861578Z",
  "city": "Medellín", "neighborhood": "Manila",
  "address": "Transversal 80A # 81-57 Apto 206",
  "property_type": "APARTMENT", "area_m2": "83.80",
  "bedrooms": 1, "bathrooms": 1
}
```

#### `GET /listings/{listing_id}`
One listing. **404** if not in your agency.

#### `POST /listings/{listing_id}/views`
Append-only page-view event; feeds `analytics.listing_performance.views`.
`client_id` is optional — anonymous traffic still counts.

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"session_id":"web-abc123","client_id":null}' \
  $BASE/listings/$LISTING/views
```

---

### 6.2 Leads — the funnel

#### `POST /clients` — register, or return by Telegram account
A lead needs a `client_id`; this is where one comes from (migration `008`).

```bash
curl -s -w '\nHTTP %{http_code}\n' -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "full_name": "Ana Gómez",
  "phone": "3001234567",
  "email": null,
  "telegram_user_id": 5712345678
}' $BASE/clients
# -> {"id": "…", "created_at": "…"}
```

- **With `telegram_user_id`**: one Telegram account is one client. **201** the
  first time, **200** with the same `id` after that. The stored name and phone
  are *not* updated by a repeat. Enforced by a UNIQUE index, so two simultaneous
  first messages cannot create two clients.
- **Without it**: always **201**, always a new client. Name, email and phone are
  never used to match. In the live data they collide between different people
  (and phones get reformatted), so a match would merge strangers.
  `docs/DECISIONS.md` §18.

`full_name` is required (1–200 chars). `phone` ≤ 40, `email` ≤ 320, both optional.
The response is only `id` + `created_at`: clients are shared across agencies, so
contact details are never echoed back. Service accounts need `clients:create`.

#### `POST /leads` — create or return
HU-01 CA3. A `(client_id, listing_id)` pair is one conversation.

```bash
curl -s -w '\nHTTP %{http_code}\n' -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "client_id":  "00059b9d-a87d-41ff-b1fa-7e6afa236dbb",
  "listing_id": "626d719d-81cf-4252-9815-9d6c9d713084",
  "source_channel": "TELEGRAM",
  "message": "¿Sigue disponible?"
}' $BASE/leads
```

- **201** — new thread created. The opening `INTERESTED` transition is written,
  and if `message` is present it lands as the first `INBOUND` interaction (this
  is what starts the response-time clock).
- **200** — the thread already existed; the existing lead is returned (same `id`).
- **404** — the listing is not in your agency, or the client does not exist.

`source_channel`: `TELEGRAM` \| `IN_APP` \| `CALL`. `message` ≤ 4000 chars, optional.

```json
{
  "id": "5e12fb60-e1cd-471c-bea8-75bd6ca20b93",
  "client_id": "00059b9d-a87d-41ff-b1fa-7e6afa236dbb",
  "listing_id": "626d719d-81cf-4252-9815-9d6c9d713084",
  "agent_id": "de8a774b-cf36-458b-8737-819096da1dac",
  "source_channel": "TELEGRAM",
  "current_stage": "INTERESTED",
  "created_at": "2026-09-01T18:48:27.689803Z",
  "updated_at": "2026-09-01T18:48:27.689803Z"
}
```
The lead's `agent_id` is **the listing's agent** — you don't choose it.

#### `GET /leads` — the board
All leads in your agency, newest activity first.

| query | |
|---|---|
| `stage` | filter by `current_stage` (`INTERESTED` … `LOST`) |
| `agent_id` | filter by owning agent |
| `listing_id` | filter by listing |
| `limit` / `offset` | pagination |

#### `GET /leads/{lead_id}`
One lead. **404** outside your agency.

#### `GET /leads/at-risk` — going cold
Non-terminal leads with **no `OUTBOUND` interaction** inside the inactivity
window (also created before the cutoff). Same predicate the hourly sweep uses.

| query | |
|---|---|
| `hours` | override `INACTIVITY_HOURS` (1–8760) |
| `limit` | 1–500, default 100 |

---

### 6.3 Funnel transitions

#### `GET /leads/{lead_id}/transitions`
The append-only stage log — the **source of truth** for the funnel.
`lead.current_stage` is a trigger-maintained cache of the newest row.

#### `POST /leads/{lead_id}/transitions` — move the lead

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to_stage":"VISIT_SCHEDULED","note":"Client confirmed Saturday"}' \
  $BASE/leads/$LEAD/transitions
```

**Legal edges** (enforced in the service layer, not the schema):

| from | allowed `to_stage` |
|---|---|
| `INTERESTED` | `VISIT_SCHEDULED`, `LOST` |
| `VISIT_SCHEDULED` | `VISITED`, `LOST` |
| `VISITED` | `NEGOTIATING`, `LOST` |
| `NEGOTIATING` | `WON`, `LOST` |
| `WON` / `LOST` | *(terminal — nothing)* |

Body:

| field | rule |
|---|---|
| `to_stage` | one of the six stages |
| `lost_reason` | **required iff** `to_stage == "LOST"`; else must be omitted. One of `PRICE`, `LOCATION`, `BOUGHT_ELSEWHERE`, `NO_RESPONSE`, `FINANCING`, `OTHER` |
| `note` | optional ≤ 2000 chars; recorded as an `OUTBOUND` `STATUS_CHANGE` interaction |

Errors:
- **409** — illegal edge, or the lead is already terminal
- **422** — `LOST` without `lost_reason`, `lost_reason` on a non-`LOST` move, or an unknown reason code

Moving to `LOST` also writes a `lead_lost_detail` row in the **same transaction** —
either both land or neither does.

```json
{
  "id": "646ac7fe-f62c-44ce-894e-2aa66767a8a3",
  "lead_id": "5e12fb60-e1cd-471c-bea8-75bd6ca20b93",
  "from_stage": "INTERESTED",
  "to_stage": "VISIT_SCHEDULED",
  "changed_by": "4458a885-ab90-49a4-b5a2-40ae5af30553",
  "changed_at": "2026-09-01T18:46:17.973526Z"
}
```

---

### 6.4 Interaction timeline

#### `GET /leads/{lead_id}/interactions`
The conversation, oldest first.

#### `POST /leads/{lead_id}/interactions`
Log a message, call, or note. **Log agent replies here** — the first `OUTBOUND`
interaction is what the response-time metric measures.

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "direction": "OUTBOUND",
  "channel": "TELEGRAM",
  "type": "MESSAGE",
  "body": "Hola, sí está disponible. ¿Le sirve el sábado a las 10?"
}' $BASE/leads/$LEAD/interactions
```

| field | values |
|---|---|
| `direction` | `INBOUND` \| `OUTBOUND` |
| `channel` | `TELEGRAM` \| `IN_APP` \| `CALL` |
| `type` | `MESSAGE` \| `CALL` \| `NOTE` \| `STATUS_CHANGE` (default `MESSAGE`) |
| `body` | ≤ 4000 chars, optional |
| `occurred_at` | optional; backdate an interaction |

`created_by` is set to the acting agent for `OUTBOUND`, left null for `INBOUND`.

---

### 6.5 Follow-up tasks

#### `GET /leads/{lead_id}/tasks`
Tasks for the lead, earliest due first.

#### `POST /leads/{lead_id}/tasks`
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"due_at":"2026-09-05T14:00:00Z","note":"Call back re: financing"}' \
  $BASE/leads/$LEAD/tasks
```
`due_at` required; `note` ≤ 1000 chars.

#### `PATCH /leads/{lead_id}/tasks/{task_id}`
Complete, snooze, or edit. Provide at least one of `status`
(`PENDING` \| `DONE` \| `SNOOZED`), `due_at`, `note`. **404** if the task is not
on that lead.

> The inactivity sweep and the `lead.created` event handler raise tasks
> automatically — see §7 and §8.

---

### 6.6 Reassignment — `TEAM_ADMIN` only

#### `POST /leads/{lead_id}/reassign`
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to_agent_id":"de8a774b-cf36-458b-8737-819096da1dac"}' \
  $BASE/leads/$LEAD/reassign
```
Updates `lead.agent_id` **and** writes an `assignment_audit` row in one
transaction (doing only one was the seeder's original bug).

- **403** — caller is not a `TEAM_ADMIN`
- **404** — target agent is not an active agent in your agency
- **409** — target is deactivated, or already owns the lead

---

### 6.7 Appointments (visits)

Availability tables were originally cut, so this is **request → the agent
confirms**, not book-a-free-slot.

#### `POST /leads/{lead_id}/appointments` — request a visit
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scheduled_at":"2026-09-07T16:00:00Z","duration_min":60}' \
  $BASE/leads/$LEAD/appointments
```
Lands as `PENDING_CONFIRMATION`. `duration_min` 15–480, default 60.

- **201** — created
- **409** — overlaps a visit the agent already has. Back-to-back is fine
  (half-open intervals: a visit ending 11:00 doesn't block one starting 11:00).
  Double-booking prevention is a per-agent advisory lock + `SELECT … FOR UPDATE`,
  so it's safe under concurrency — fire four identical requests and exactly one wins.
- **422** — `scheduled_at` is in the past
- **409** *(only when `ENFORCE_AVAILABILITY=true`)* — the slot is outside the
  agent's published availability

#### `GET /leads/{lead_id}/appointments`
Visits for the lead, earliest first.

#### `GET /appointments/{appointment_id}`
One appointment. **404** outside your agency.

#### `PATCH /appointments/{appointment_id}` — lifecycle
```bash
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"CONFIRMED"}' $BASE/appointments/$APPT
```
> `curl` sends `POST` when you pass `-d` — **`-X PATCH` is required** on every
> PATCH call in this guide, or you get a `405`.

Set `status` and/or move it in time with `scheduled_at` / `duration_min`
(a move re-runs the overlap check). Provide at least one field.

| status | meaning |
|---|---|
| `PENDING_CONFIRMATION` | initial |
| `CONFIRMED` | agent accepted |
| `RESCHEDULED` | moved (auto-set if you move a `CONFIRMED` visit without naming a status) |
| `CANCELLED` / `COMPLETED` / `NO_SHOW` | **terminal** |

- **409** — already terminal, or the new slot overlaps
- **422** — new `scheduled_at` in the past

#### `POST /appointments/{appointment_id}/feedback`
Only valid once the visit is `COMPLETED`.

```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "submitted_by": "AGENT",
  "interest_score": 4,
  "objection": "PRICE",
  "close_probability": 0.6,
  "free_text": "Loved it, wants 5% off"
}' $BASE/appointments/$APPT/feedback
```

| field | rule |
|---|---|
| `submitted_by` | `AGENT` \| `CLIENT` |
| `interest_score` | 1–5, optional |
| `objection` | `PRICE`, `SIZE`, `LOCATION`, `CONDITION`, `HOA_FEE`, `OTHER` — optional |
| `close_probability` | 0–1, optional |
| `free_text` | ≤ 2000 chars, optional |

- **409** — the visit is not `COMPLETED`
- **422** — unknown objection code

---

### 6.8 Agent availability (HU-05)

Weekly rules + ad-hoc time off. Turning them into bookable slots is
`GET /slots`. All endpoints are agency-scoped through `core.agent` — an agent in
another agency is **404**.

#### `GET /agents/{agent_id}/availability`
The weekly rules.

#### `POST /agents/{agent_id}/availability`
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "weekday": 0,
  "start_time": "09:00",
  "end_time": "12:00"
}' $BASE/agents/$AGENT/availability
```

| field | rule |
|---|---|
| `weekday` | **0 = Monday … 6 = Sunday** |
| `start_time` / `end_time` | `HH:MM` local (`APP_TIMEZONE`, default `America/Bogota`); `start < end` |
| `valid_from` | date, defaults to today |
| `valid_to` | date, optional; must not precede `valid_from` |

#### `PATCH /agents/{agent_id}/availability/{rule_id}`
Change any of `weekday`, `start_time`, `end_time`, `valid_from`, `valid_to`.

#### `DELETE /agents/{agent_id}/availability/{rule_id}` → **204**

#### `GET` / `POST` / `DELETE /agents/{agent_id}/time-off`
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "starts_at": "2026-09-07T15:00:00Z",
  "ends_at":   "2026-09-07T15:30:00Z",
  "reason": "dentist"
}' $BASE/agents/$AGENT/time-off
```
Half-open `[starts_at, ends_at)`; `starts_at < ends_at`. `DELETE` returns **204**.

#### `GET /agents/{agent_id}/slots?from=&to=` — free 30-minute grid
```bash
curl -s -H "$AUTH" \
  "$BASE/agents/$AGENT/slots?from=2026-09-07T00:00:00Z&to=2026-09-08T00:00:00Z"
```
```json
{
  "agent_id": "451b4cf3-6123-4df7-b656-af7229d4beef",
  "slot_minutes": 30,
  "slots": [
    "2026-09-07T14:00:00Z", "2026-09-07T14:30:00Z",
    "2026-09-07T15:30:00Z", "2026-09-07T16:00:00Z", "2026-09-07T16:30:00Z"
  ]
}
```
The weekly rules are expanded over `[from, to)` in `APP_TIMEZONE`, then
**time off** and **calendar-blocking appointments** (`PENDING_CONFIRMATION`,
`CONFIRMED`, `RESCHEDULED`) are subtracted. `from` and `to` are `?from=`/`?to=`
query params (ISO-8601); `from < to`, else **422**.

---

### 6.9 Analytics — `analytics.*` views only, never `core`

Every response is already scoped to your agency.

#### `GET /analytics/north-star` — the five metrics
```json
{
  "leads": 554,
  "median_first_response_hours": 1.92,
  "pct_with_follow_up": 9.93,
  "lead_to_visit_conversion_pct": 25.63,
  "pct_lost_within_48h": 18.95,
  "stage_conversion": [
    {"stage":"INTERESTED","sort_order":1,"leads_reached":554,"leads_prev_stage":null,"pct_from_prev":null},
    {"stage":"VISIT_SCHEDULED","sort_order":2,"leads_reached":184,"leads_prev_stage":554,"pct_from_prev":33.21},
    {"stage":"VISITED","sort_order":3,"leads_reached":142,"leads_prev_stage":184,"pct_from_prev":77.17},
    {"stage":"NEGOTIATING","sort_order":4,"leads_reached":62,"leads_prev_stage":142,"pct_from_prev":43.66},
    {"stage":"WON","sort_order":5,"leads_reached":37,"leads_prev_stage":62,"pct_from_prev":59.68},
    {"stage":"LOST","sort_order":6,"leads_reached":496,"leads_prev_stage":null,"pct_from_prev":null}
  ]
}
```
`LOST` sits off the funnel: reported as its own row, never a denominator.

#### `GET /analytics/agent-response-time`
Median/avg hours to first `OUTBOUND`, and how many leads were never answered.
Slowest first — on seeded data the injected slow cohort shows here.
```json
{
  "agent_id": "663f1c97-9562-49f0-a5d7-b8756dadd6c7",
  "agent_name": "Marcela Vargas Londoño",
  "leads": 113,
  "avg_first_response_hours": 3.37,
  "median_first_response_hours": 2.52,
  "never_answered": 25
}
```

#### `GET /analytics/funnel-daily?days=90`
Transition counts per day and target stage. `days` 1–730, default 90.

#### `GET /analytics/listing-performance?limit=&offset=`
Views, leads, visits, wins per listing, ordered by views. On seeded data the
overpriced cohort shows high views with a low win rate.

---

## 7. Automatic background work

Two jobs, implemented as SQL functions (`migrations/005_cron_jobs.sql`) and run
by **pg_cron inside Supabase** — so they keep running while the API container
is asleep on a free host. Nothing to configure on the API side.

| job | runs | what it does |
|---|---|---|
| `core.sweep_inactive_leads(72)` | hourly (`0 * * * *`) | for every non-terminal lead with no `OUTBOUND` interaction in 72 h and no `PENDING` task: raise a follow-up task (due +24 h) + an `OUTBOUND` `NOTE`, and emit `lead.went_cold`. Idempotent — a lead with a `PENDING` task is skipped. Max 500 leads per run |
| `events.relay_domain_events()` | every 2 min | publish unpublished `events.domain_event` rows (see §8) |

On the **local compose profile** there is no pg_cron, so the API's in-process
scheduler (`ENABLE_SCHEDULER=true`, `app/jobs.py`) calls the same two functions
on the same cadence. Never turn that on against Supabase — pg_cron already runs
them and you would raise every follow-up twice.

To see the jobs and their last runs on Supabase (SQL Editor):

```sql
select jobname, schedule, active from cron.job;
select jobname, status, start_time, return_message
from cron.job_run_details d join cron.job j on j.jobid = d.jobid
order by start_time desc limit 10;
```

---

## 8. Domain events (the outbox)

Kafka was cut. Instead, `services/events.emit()` writes a row to
`events.domain_event` **in the same transaction as the business change**, so an
event exists if and only if the change committed. The 30-second relay
(`events.relay_domain_events()`, pg_cron) then runs the handler for each
unpublished row and stamps `published_at`, taking its batch `for update skip
locked` so two runners can never publish the same event twice.

| `event_type` | emitted when | `aggregate_id` | payload keys | handler |
|---|---|---|---|---|
| `lead.created` | a new lead thread is created (not on the 200 dedup path) | lead id | `listing_id`, `client_id`, `agent_id` | raises the **first-touch follow-up task** |
| `lead.stage_changed` | any transition | lead id | `from`, `to` | — |
| `appointment.booked` | a visit is requested | appointment id | `lead_id`, `agent_id`, `scheduled_at` | — |
| `lead.went_cold` | the inactivity sweep flags a lead | lead id | `inactivity_hours` | — |

On a Supabase deploy the table is on the `supabase_realtime` publication with
RLS + an agency policy, so a dashboard can subscribe to its own agency's events.
That `SELECT` grant to `authenticated` is the **only** grant on any of the four
schemas — everything else stays closed and authorization stays in the service
layer.

There is no HTTP endpoint for events; read them from the database or Realtime:

```bash
docker compose exec db psql -U postgres -c \
  "select event_type, published_at is not null as published, payload
   from events.domain_event order by id desc limit 10"
```

---

## 9. Error reference

| Code | Meaning | Typical cause |
|---|---|---|
| **200** | OK / dedup hit | `POST /leads` for an existing `(client, listing)` |
| **201** | Created | new lead, transition, appointment, task, interaction, feedback, availability |
| **204** | No content | `DELETE` of an availability rule / time-off entry |
| **400** | Bad request | service-account token without `X-Agency-Id`, or a non-UUID header |
| **401** | Not authenticated | missing/expired token; `DEV_AUTH_BYPASS` on but no `X-Dev-Agent-Id` |
| **403** | Authenticated, not allowed | token not bound to an agent; non-`TEAM_ADMIN` calling reassign; deactivated agent or service account; service account lacks the route's scope |
| **404** | Not found *or not yours* | lead/listing/appointment/agent in another agency; unknown id; using another agency's listing on `POST /leads` |
| **409** | Conflict | illegal/terminal transition; overlapping visit; terminal appointment; feedback on a non-`COMPLETED` visit; reassign to a deactivated / current / AI agent |
| **422** | Unprocessable | past `scheduled_at`; `LOST` without `lost_reason`; unknown enum code; `from >= to` on slots; Pydantic body validation |
| **429** | Budget spent | a service account past its `hourly_write_limit`; retry after the hour |
| **503** | DB unreachable | `/health` when Postgres is down |

Body shape: `{"detail": "..."}` (string) for app errors, or
`{"detail": [ {loc, msg, type} ]}` for Pydantic validation.

---

## 10. Enum reference

| set | values |
|---|---|
| Stage | `INTERESTED`, `VISIT_SCHEDULED`, `VISITED`, `NEGOTIATING`, `WON`, `LOST` |
| Channel | `TELEGRAM`, `IN_APP`, `CALL` |
| Interaction direction | `INBOUND`, `OUTBOUND` |
| Interaction type | `MESSAGE`, `CALL`, `NOTE`, `STATUS_CHANGE` |
| Appointment status | `PENDING_CONFIRMATION`, `CONFIRMED`, `RESCHEDULED`, `CANCELLED`, `COMPLETED`, `NO_SHOW` |
| Task status | `PENDING`, `DONE`, `SNOOZED` |
| Operation type | `SALE`, `RENT` |
| Listing status | `ACTIVE`, `PAUSED`, `CLOSED` |
| Lost reason | `PRICE`, `LOCATION`, `BOUGHT_ELSEWHERE`, `NO_RESPONSE`, `FINANCING`, `OTHER` |
| Objection | `PRICE`, `SIZE`, `LOCATION`, `CONDITION`, `HOA_FEE`, `OTHER` |
| `submitted_by` | `AGENT`, `CLIENT` |
| Agent role | `AGENT`, `TEAM_ADMIN` |
| weekday | `0` Mon … `6` Sun |

---

## 11. End-to-end walkthrough

A full funnel run against the local stack. Needs `jq`.

```bash
set -euo pipefail
BASE=http://localhost:8000
ADMIN=$(docker compose exec -T db psql -U postgres -tA -c \
  "select id from core.agent where role='TEAM_ADMIN'
   and agency_id in (select id from core.agency where name not like 'pytest-%') limit 1")
AUTH="X-Dev-Agent-Id: $ADMIN"

# 1. context
curl -s -H "$AUTH" $BASE/me | jq '{agent: .full_name, agency_id}'
LISTING=$(curl -s -H "$AUTH" "$BASE/listings?limit=1" | jq -r '.[0].id')
AGENT=$(curl -s -H "$AUTH" "$BASE/listings?limit=1" | jq -r '.[0].agent_id')
CLIENT=$(docker compose exec -T db psql -U postgres -tA -c \
  "select c.id from core.client c
   where not exists (select 1 from core.lead l
                     where l.client_id=c.id and l.listing_id='$LISTING') limit 1")

# 2. inbound contact -> lead (201)
LEAD=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' -d "{
  \"client_id\":\"$CLIENT\",\"listing_id\":\"$LISTING\",
  \"source_channel\":\"TELEGRAM\",\"message\":\"¿Sigue disponible?\"}" \
  $BASE/leads | jq -r '.id')
echo "lead = $LEAD"

# 3. same contact again -> dedup (200, same id)
curl -s -o /dev/null -w "re-contact: HTTP %{http_code}\n" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d "{\"client_id\":\"$CLIENT\",\"listing_id\":\"$LISTING\",\"source_channel\":\"TELEGRAM\"}" \
  $BASE/leads

# 4. agent replies (starts the response-time clock)
curl -s -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"direction":"OUTBOUND","channel":"TELEGRAM","type":"MESSAGE","body":"Sí, ¿el sábado a las 10?"}' \
  $BASE/leads/$LEAD/interactions

# 5. publish availability, read the free slots
for d in 0 1 2 3 4 5; do
  curl -s -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"weekday\":$d,\"start_time\":\"09:00\",\"end_time\":\"13:00\"}" \
    $BASE/agents/$AGENT/availability
done
curl -s -H "$AUTH" "$BASE/agents/$AGENT/slots?from=2026-09-07T00:00:00Z&to=2026-09-08T00:00:00Z" \
  | jq '.slots'

# 6. move to VISIT_SCHEDULED and request the visit
curl -s -o /dev/null -w "-> VISIT_SCHEDULED: HTTP %{http_code}\n" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"to_stage":"VISIT_SCHEDULED"}' \
  $BASE/leads/$LEAD/transitions
APPT=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scheduled_at":"2026-09-07T16:00:00Z","duration_min":60}' \
  $BASE/leads/$LEAD/appointments | jq -r '.id')

# 7. confirm -> complete -> feedback   (-X PATCH: curl POSTs otherwise)
curl -s -o /dev/null -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"CONFIRMED"}' $BASE/appointments/$APPT
curl -s -o /dev/null -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"COMPLETED"}' $BASE/appointments/$APPT
curl -s -o /dev/null -w "feedback: HTTP %{http_code}\n" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"submitted_by":"AGENT","interest_score":4,"objection":"PRICE","close_probability":0.6}' \
  $BASE/appointments/$APPT/feedback

# 8. walk the rest of the funnel to WON
for s in VISITED NEGOTIATING WON; do
  curl -s -o /dev/null -w "-> $s: HTTP %{http_code}\n" -H "$AUTH" \
    -H 'Content-Type: application/json' -d "{\"to_stage\":\"$s\"}" \
    $BASE/leads/$LEAD/transitions
done

# 9. the events this produced (after the 30s relay tick)
sleep 32
docker compose exec -T db psql -U postgres -c \
  "select event_type, published_at is not null as published
   from events.domain_event
   where aggregate_id='$LEAD' or payload->>'lead_id'='$LEAD' order by id"

# 10. one first-touch task, from the lead.created handler
curl -s -H "$AUTH" $BASE/leads/$LEAD/tasks | jq 'length, .[].note'

# 11. metrics moved
curl -s -H "$AUTH" $BASE/analytics/north-star \
  | jq '{leads, lead_to_visit_conversion_pct, median_first_response_hours}'
```

---

## 12. Recipes

**Losing a lead** (needs a reason):
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"to_stage":"LOST","lost_reason":"BOUGHT_ELSEWHERE","note":"Closed with another agency"}' \
  $BASE/leads/$LEAD/transitions
```

**The at-risk queue**, then nudge one:
```bash
curl -s -H "$AUTH" "$BASE/leads/at-risk?hours=48&limit=20" | jq '.[].id'
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"direction":"OUTBOUND","channel":"CALL","type":"CALL","body":"Left a voicemail"}' \
  $BASE/leads/$LEAD/interactions
```

**Rescheduling a confirmed visit** (auto-becomes `RESCHEDULED`, re-checks overlap):
```bash
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scheduled_at":"2026-09-08T15:00:00Z"}' $BASE/appointments/$APPT
```

**Only book slots the agent published** — set `ENFORCE_AVAILABILITY=true` on the
API, then `POST /leads/{id}/appointments` returns **409** for any slot not in
`GET /agents/{id}/slots`.

**Board filtered to one agent's live deals:**
```bash
curl -s -H "$AUTH" "$BASE/leads?agent_id=$AGENT&stage=NEGOTIATING" | jq
```

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

   # the bot answers
   curl -s "${H[@]}" $API_BASE/leads/$LEAD/interactions \
     -d '{"direction":"OUTBOUND","channel":"TELEGRAM","body":"¡Hola! Sí, sigue disponible."}'

   # ...the client wants to visit: offer free times from the lead's agent
   # (lead.agent_id), at least 2 h out, long enough for a 60-min visit
   curl -s "${H[@]}" "$API_BASE/agents/$LEAD_AGENT/slots?from=2026-09-14T13:00:00Z&to=2026-09-21T00:00:00Z&duration_min=60"

   # ...the client picks one -> PENDING_CONFIRMATION (201); the same post again -> 200
   curl -s "${H[@]}" $API_BASE/leads/$LEAD/appointments \
     -d '{"scheduled_at":"2026-09-15T15:00:00Z","duration_min":60}'

   # ...and gets it for their calendar: the .ics as a Telegram document, plus
   # the google_calendar_url from the visit's detail (Android can't open .ics)
   curl -s "${H[@]}" $API_BASE/appointments/$APPT/invite.ics -o visita.ics
   curl -s "${H[@]}" $API_BASE/appointments/$APPT | jq -r .google_calendar_url
   ```

   Do **not** post a `VISIT_SCHEDULED` transition yourself: the lead moves
   there when its owner confirms the visit. The whole scheduling conversation —
   returning clients, moving and cancelling, feedback after the visit — is
   **§6.10 "How the bot schedules"**.

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
`tasks:write`, `visits:request` (book, move and cancel visits), `clients:create`
(added by `008`), `visits:feedback` (the client's post-visit feedback, added by
`009`; both migrations also granted them to existing accounts). Not granted by
default: `leads:close` (moving a lead to `WON`/`LOST` — on top of
`leads:transition`), `visits:manage` (confirming a visit — granting it makes the
bot's bookings land `CONFIRMED`, i.e. instant booking), `availability:write`,
`calendar:feed`, `listings:views`. Recording `COMPLETED`/`NO_SHOW` is for people
only, whatever the scopes. Reassign stays `TEAM_ADMIN`-only, so a bot can never
do it. Grant a scope with one UPDATE, no deploy:

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
| Visit | `core.appointment` | **the calendar** — one row per visit; the owner confirms; confirming / completing moves the lead's stage; feedback after |
| Availability | `core.agent_availability` / `agent_time_off` | weekly rules + time off (manual, or busy time imported from the agent's own calendar) |
| The agent's own calendar | `core.agent_external_calendar` | a Google/Outlook/iCloud iCal address, read for busy time only (§6.10) |
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
One **card** per lead in your agency, newest activity first (HU-06). A card is
the lead (`LeadOut`) plus what an agent needs at a glance, all from one query:

| card field | |
|---|---|
| `client_name`, `listing_address`, `neighborhood` | who and what the lead is about |
| `operation_type`, `asking_price` | `SALE` / `RENT` and the listing price |
| `last_interaction` | newest timeline entry — `{occurred_at, direction, type, body}`, `body` cut to 140 chars — or `null` if the thread is empty. Any entry counts, including status-change notes; it shows what last happened, not the last *response* |

| query | |
|---|---|
| `stage` | filter by `current_stage` (`INTERESTED` … `LOST`) |
| `agent_id` | filter by owning agent |
| `listing_id` | filter by one listing |
| `property_id` | filter by the physical property — covers its SALE and RENT listings |
| `client_id` | filter by client — how the bot finds a returning client's threads |
| `created_from` / `created_to` | `YYYY-MM-DD`, both inclusive, read in the agency timezone (`America/Bogota`). **422** if `created_from` is after `created_to` |
| `active` | `true` = the working board: hides `WON` and `LOST`. Default `false` (everything). Closed leads stay reachable with `stage=WON` / `stage=LOST` (HU-09) |
| `limit` / `offset` | pagination |

A Kanban column is `GET /leads?active=true&stage=<STAGE>`; moving a card is
`POST /leads/{id}/transitions`.

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
either both land or neither does — and always adds a `STATUS_CHANGE` line to the
timeline, `Lost: <reason>` (plus ` — <note>` when a note was sent), so the reason
stays readable in the lead's history. Other stages add that line only when a
`note` is sent. `STATUS_CHANGE` never counts as an agent response.

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
- **409** — target is deactivated, already owns the lead, or is an AI agent

The new owner is **not notified** — no event, no push (HU-08 AC2 is cut by
decision, `docs/DECISIONS.md` §20). The lead simply appears on their board
(`GET /leads?agent_id=`); the history is `assignment_audit`.

---

### 6.7 Appointments (visits)

`core.appointment` **is** the calendar (DECISIONS §19). Three rules decide
everything below:

- **Status is the owning agent's consent.** A visit booked by the lead's owner
  is born `CONFIRMED`. One booked by anybody else — an AI agent on the client's
  behalf, a colleague — lands `PENDING_CONFIRMATION` until the owner confirms
  (HU-02). An AI agent granted `visits:manage` books `CONFIRMED` (instant booking).
- **One open visit per lead.** Posting the same slot again returns the visit
  already made (**200**) — safe to retry. Any other time is a **409**: move the
  open visit with `PATCH` instead.
- **The calendar drives the funnel.** `CONFIRMED` moves an `INTERESTED` lead to
  `VISIT_SCHEDULED`; `COMPLETED` moves a `VISIT_SCHEDULED` lead to `VISITED`;
  moving a lead to `WON`/`LOST` cancels its open visits. Written as ordinary
  transitions (`changed_by` = whoever acted), so don't post those yourself.

Every change also writes one `STATUS_CHANGE` line on the lead's timeline,
attributed to whoever made it, and one `appointment.*` event (§8).

#### `POST /leads/{lead_id}/appointments` — book a visit
```bash
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scheduled_at":"2026-09-15T15:00:00Z","duration_min":60}' \
  $BASE/leads/$LEAD/appointments
```
`duration_min` 15–480, default 60. The response carries `created_by` — who booked it.

- **201** — booked (`CONFIRMED` or `PENDING_CONFIRMATION`, per the consent rule)
- **200** — the lead already had exactly this visit; it is returned unchanged
- **409** — overlaps a visit the agent already has; or the lead already has an
  open visit at another time; or the lead is `WON`/`LOST`. Back-to-back is fine
  (half-open intervals: a visit ending 11:00 doesn't block one starting 11:00).
  Double-booking prevention is a per-agent advisory lock + `SELECT … FOR UPDATE`,
  so it's safe under concurrency — four clients asking for one slot, one wins.
- **422** — `scheduled_at` is in the past
- **AI agents only:** **409** if the slot is not in `GET /agents/{id}/slots`
  (published hours minus time off minus visits), **422** if it starts sooner
  than `VISIT_MIN_NOTICE_MINUTES` (120). For people that check runs only when
  `ENFORCE_AVAILABILITY=true` (default false).

#### `GET /leads/{lead_id}/appointments`
Visits for the lead, earliest first.

#### `GET /appointments/{appointment_id}`
One visit plus where it is. **404** outside your agency.
```json
{
  "id": "03c2fe87-…", "lead_id": "dd78c6cd-…", "agent_id": "663f1c97-…",
  "scheduled_at": "2026-09-15T16:00:00Z", "duration_min": 60, "status": "CONFIRMED",
  "created_by": "663f1c97-…", "created_at": "…", "updated_at": "…",
  "listing_id": "acf5e81e-…",
  "location": "Carrera 25A # 86-49, Santa María de los Ángeles, Medellín",
  "agent_name": "Marcela Vargas Londoño",
  "google_calendar_url": "https://calendar.google.com/calendar/render?action=TEMPLATE&text=Visita+inmobiliaria+…"
}
```

#### `GET /appointments/{appointment_id}/invite.ics` — the client's copy
The visit as a one-event `.ics` (`text/calendar`), for the bot to send the client
as a document. Opens in Apple Calendar, Outlook and desktop calendars; on
Android use `google_calendar_url` instead. Contains the address, the agent's name
and the status — nothing about the client. Same UID
(`<appointment id>@homelitics`) every time, so re-sending it after a change
updates the event in Apple/Outlook rather than adding a second one.

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
| `PENDING_CONFIRMATION` | set by someone other than the owner; the owner hasn't agreed yet |
| `CONFIRMED` | the owning agent agreed to this time |
| `RESCHEDULED` | moved, awaiting re-confirmation (auto-set when a `CONFIRMED` visit is moved without naming a status — the owner confirms their own move by sending `"status":"CONFIRMED"` with it) |
| `CANCELLED` / `COMPLETED` / `NO_SHOW` | **terminal** |

- **403** — an AI agent confirming without `visits:manage`, or recording
  `COMPLETED`/`NO_SHOW` (people only). AI agents may move and cancel.
- **409** — already terminal, or the new slot overlaps (or, for an AI agent, is
  outside the published slots)
- **422** — new `scheduled_at` in the past (or, for an AI agent, too soon)

#### `GET /appointments/{appointment_id}/feedback`
Feedback on the visit — at most one row per side (`AGENT`, `CLIENT`). The bot
reads it to know whether the client has already been asked.

#### `POST /appointments/{appointment_id}/feedback`
Only valid once the visit is `COMPLETED`. One per side: posting again from the
same side returns the first row with **200**. An AI agent may only submit
`"submitted_by": "CLIENT"` (**403** otherwise; scope `visits:feedback`).

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

- **409** on `POST` — the time off overlaps a visit that is still on the
  calendar; the message lists them. A booked visit is a commitment: move or
  cancel it first (and tell the client).
- **409** on `DELETE` — the block has `"source": "ICS"`: it was imported from the
  agent's own calendar (§6.10), so remove it there.

#### `GET /agents/{agent_id}/slots?from=&to=&duration_min=` — free times for a visit
```bash
curl -s -H "$AUTH" \
  "$BASE/agents/$AGENT/slots?from=2026-09-07T00:00:00Z&to=2026-09-08T00:00:00Z&duration_min=60"
```
```json
{
  "agent_id": "451b4cf3-6123-4df7-b656-af7229d4beef",
  "slot_minutes": 30,
  "duration_min": 60,
  "slots": [
    "2026-09-07T14:00:00Z", "2026-09-07T15:30:00Z", "2026-09-07T16:00:00Z"
  ]
}
```
The weekly rules are expanded over `[from, to)` in `APP_TIMEZONE` on a 30-minute
grid, then **time off** (manual and imported) and **calendar-blocking
appointments** (`PENDING_CONFIRMATION`, `CONFIRMED`, `RESCHEDULED`) are
subtracted. A start is listed only if a whole visit of `duration_min` (15–480,
default 30) fits there, and only if it is in the future. This is exactly what
an AI agent may book. `from` and `to` are ISO-8601 — write UTC as `Z`, since a
`+` in a query string reads as a space. `from < to`, else **422**.

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

#### `GET /analytics/lost-reasons?days=90`
Why leads were lost (HU-09): leads lost in the last `days` (1–730, default 90,
counted on the loss date), grouped by the reason given when they moved to `LOST`,
most common first. `pct` is the share of those lost leads, so the rows sum to
100; a reason nobody used is absent, not zero. Reads `analytics.lead_outcome.lost_reason`
(migration `010`).

```json
[
  {"reason": "PRICE",            "leads": 49, "pct": 23.11},
  {"reason": "NO_RESPONSE",      "leads": 43, "pct": 20.28},
  {"reason": "BOUGHT_ELSEWHERE", "leads": 33, "pct": 15.57}
]
```

---

### 6.10 Calendar (migration `009`)

The visits of §6.7 *are* the calendar; these routes only show them, or feed an
agent's own busy time in. Everything leaves in UTC (`Z`); render it in
`America/Bogota`, which every calendar response names in `timezone`.

#### `GET /agents/{agent_id}/calendar?from=&to=` — one agent, for the frontend
```bash
curl -s -H "$AUTH" "$BASE/agents/$AGENT/calendar?from=2026-09-14T00:00:00Z&to=2026-09-21T00:00:00Z"
```
```json
{
  "timezone": "America/Bogota",
  "events": [
    {"id": "availability-663f…-20260914T1400", "kind": "AVAILABILITY",
     "agent_id": "663f…", "start": "2026-09-14T14:00:00Z", "end": "2026-09-14T17:00:00Z",
     "title": "Disponible"},
    {"id": "03c2fe87-…", "kind": "VISIT", "agent_id": "663f…", "agent_name": "Marcela Vargas Londoño",
     "start": "2026-09-15T16:00:00Z", "end": "2026-09-15T17:00:00Z",
     "title": "Visita · Susana · Santa María de los Ángeles (por confirmar)",
     "status": "PENDING_CONFIRMATION", "lead_id": "dd78…", "listing_id": "acf5…",
     "location": "Carrera 25A # 86-49, Santa María de los Ángeles, Medellín",
     "booked_by_bot": true, "conflict": false},
    {"id": "9e1a…", "kind": "TIME_OFF", "agent_id": "663f…", "agent_name": "Marcela Vargas Londoño",
     "start": "2026-09-16T14:00:00Z", "end": "2026-09-16T15:00:00Z",
     "title": "Ocupado (calendario externo)", "source": "ICS"}
  ]
}
```
- `VISIT` — every status, so the frontend can grey out cancelled ones. The
  owner's *to confirm* queue is `PENDING_CONFIRMATION` + `RESCHEDULED`.
  `conflict: true` means busy time imported from the agent's own calendar
  overlaps the visit — the agent decides.
- `TIME_OFF` — manual (`source: MANUAL`, titled with its reason) or imported
  (`source: ICS`, never with the event's own title).
- `AVAILABILITY` — published hours, meant as a background.

At most 62 days per request (**422** beyond); **404** for an agent outside your
agency. Shaped for [FullCalendar](https://fullcalendar.io): fetch with your
Bearer header, map `AVAILABILITY` to `display: 'background'`. For live updates,
subscribe to `events.domain_event` over Supabase Realtime (§8 — already granted
per agency) and refetch on `appointment.*`.

#### `GET /calendar?from=&to=` — the whole agency
Every agent's visits and time off, each with `agent_name` — the team view. No
availability background.

#### `GET /me/calendar-feed` — your visits in Google / Apple / Outlook
```json
{"ics_url": "https://homelitics-api.onrender.com/agents/663f…/calendar.ics?token=5d0c…",
 "webcal_url": "webcal://homelitics-api.onrender.com/agents/663f…/calendar.ics?token=5d0c…"}
```
Google Calendar → *Other calendars → + → From URL* → paste `ics_url`. On a
Mac/iPhone, open `webcal_url`. The feed carries your visits from 30 days back to
180 ahead (cancelled ones drop out), each titled with the client's first name,
the neighbourhood and whether it is still to be confirmed.

- **The URL is the credential** — calendar apps send no Authorization header —
  so treat it like a password. `POST /me/calendar-feed/rotate` issues a new one;
  the old URL is a **404** from then on.
- **It is a mirror, not the live view.** Google refreshes subscribed calendars
  on its own schedule — hours — and cannot be forced; Apple lets you pick (5–15
  min). On the free Render host a fetch that hits a cold start may fail and be
  retried next cycle.
- AI agents have no feed (**404**).

The feed itself is `GET /agents/{agent_id}/calendar.ics?token=…` — no auth header,
`text/calendar`; a wrong token is **404**.

#### `PUT /me/external-calendar` — your own calendar as busy time
```bash
curl -s -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"ics_url":"https://calendar.google.com/calendar/ical/you%40gmail.com/private-…/basic.ics"}' \
  $BASE/me/external-calendar
```
Where to find the address: Google Calendar → *Settings* → your calendar →
*Integrate calendar* → **Secret address in iCal format**. Outlook and iCloud
publish one too.

Every busy event in the next 60 days becomes time off (`source: ICS`), so
`/slots` stops offering those times and **the bot cannot book over them**. Only
the times are imported, never titles, attendees or descriptions. Events marked
*free*, cancelled ones and copies of our own visits are skipped. It syncs
immediately, then again whenever a calendar read finds it older than 15 minutes.

- The response masks the address (`ics_url_masked`); it is a credential for
  your whole calendar.
- **422** — not `https://`/`webcal://`, or not on Google/Outlook/iCloud (the
  API fetches it, so the hosts are an allow-list).
- A feed that fails never breaks a request: the error is recorded in
  `last_status`/`last_error`, and the busy time from the last good sync stays.

`GET /me/external-calendar` shows the status. `POST /me/external-calendar/sync`
syncs now → `{"status": "OK", "imported": 5, "removed": 0, "skipped": 3,
"error": null}`. `DELETE /me/external-calendar` disconnects it and removes
everything it imported.

#### How the bot schedules (reactive, per client message)
The bot never gets pushed anything. On each message it reads the current state
and acts in the conversation:

1. `POST /clients` with `telegram_user_id` → the client (201 new, 200 returning).
2. `GET /leads?client_id=…` with each agency header → their existing threads;
   otherwise `POST /leads`.
3. The client wants to visit → `GET /agents/{lead.agent_id}/slots?from=<now+2h>&to=<+7d>&duration_min=60`.
   Offer 3–5 times, in `America/Bogota`.
4. The client picks one → `POST /leads/{id}/appointments`. **201**
   `PENDING_CONFIRMATION`: tell them the agent will confirm and that they can
   ask any time. **200**: a retry, same visit. **409**: taken meanwhile — offer again.
5. Send `GET /appointments/{id}/invite.ics` as a document, plus
   `google_calendar_url` from `GET /appointments/{id}`.
6. Any later message → `GET /leads/{id}/appointments` and say what is true
   *now*, whatever the agent did since. The client wants another time →
   `PATCH` with `scheduled_at` (→ `RESCHEDULED`, the agent re-confirms); wants
   out → `PATCH {"status":"CANCELLED"}`.
7. A `COMPLETED` visit with no `CLIENT` row in `GET /appointments/{id}/feedback`
   → ask for a 1–5 score and the main objection → `POST .../feedback` with
   `"submitted_by": "CLIENT"`.

What the bot never does: confirm (unless granted `visits:manage`), mark
`COMPLETED`/`NO_SHOW`, post `VISIT_SCHEDULED`/`VISITED` transitions (the
calendar does that), or close a lead (unless granted `leads:close`). Agents'
changes are not pushed to clients: an agent who moves or cancels a confirmed
visit should tell the client.

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
| `appointment.booked` | a visit is booked (not on the 200 retry path) | appointment id | `lead_id`, `agent_id`, `scheduled_at`, `duration_min`, `status`, `actor_id`, `by_bot` | — |
| `appointment.confirmed` / `.cancelled` / `.completed` / `.no_show` / `.reopened` | a visit's status changes (`.cancelled` also when its lead is closed — `reason: lead_won`/`lead_lost`) | appointment id | the same, plus `previous_status`, `previous_scheduled_at` | — |
| `appointment.rescheduled` | a visit moves in time | appointment id | the same | — |
| `lead.went_cold` | the inactivity sweep flags a lead | lead id | `inactivity_hours` | — |

The `appointment.*` events are recorded for audit and for the dashboard's
Realtime feed. Nothing delivers them to clients: the bot reads the current state
when a client writes (§6.10).

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
| **200** | OK / dedup hit | `POST /leads` for an existing `(client, listing)`; `POST /clients` for a known Telegram id; the same visit or feedback posted again |
| **201** | Created | new lead, transition, appointment, task, interaction, feedback, availability |
| **204** | No content | `DELETE` of an availability rule / time-off entry |
| **400** | Bad request | service-account token without `X-Agency-Id`, or a non-UUID header |
| **401** | Not authenticated | missing/expired token; `DEV_AUTH_BYPASS` on but no `X-Dev-Agent-Id` |
| **403** | Authenticated, not allowed | token not bound to an agent; non-`TEAM_ADMIN` calling reassign; deactivated agent or service account; service account lacks the route's scope; an AI agent confirming a visit without `visits:manage`, recording `COMPLETED`/`NO_SHOW`, or submitting feedback as `AGENT` |
| **404** | Not found *or not yours* | lead/listing/appointment/agent in another agency; unknown id; using another agency's listing on `POST /leads`; a wrong calendar-feed token |
| **409** | Conflict | illegal/terminal transition; overlapping visit; another open visit on the lead; a visit on a `WON`/`LOST` lead; an AI agent outside the published slots; terminal appointment; feedback on a non-`COMPLETED` visit; time off over a booked visit; deleting imported time off; reassign to a deactivated / current / AI agent |
| **422** | Unprocessable | past `scheduled_at`; an AI agent booking too soon; `LOST` without `lost_reason`; unknown enum code; `from >= to` on slots; a calendar window over 62 days; a calendar address that is not https on an accepted host; Pydantic body validation |
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
| Agent role | `AGENT`, `TEAM_ADMIN`, `AI_AGENT` |
| weekday | `0` Mon … `6` Sun |
| Time-off source | `MANUAL`, `ICS` (imported from the agent's own calendar) |
| Calendar event kind | `VISIT`, `TIME_OFF`, `AVAILABILITY` |

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

# 5. publish availability, read the free slots for a 60-min visit in 3 days
DAY=$(python3 -c "from datetime import*;print(date.today()+timedelta(days=3))")
for d in 0 1 2 3 4 5 6; do
  curl -s -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"weekday\":$d,\"start_time\":\"09:00\",\"end_time\":\"13:00\"}" \
    $BASE/agents/$AGENT/availability
done
curl -s -H "$AUTH" "$BASE/agents/$AGENT/slots?from=${DAY}T00:00:00Z&to=${DAY}T23:59:00Z&duration_min=60" \
  | jq '.slots'

# 6. book it. The admin is not the listing's agent, so it lands PENDING_CONFIRMATION;
#    the lead stays INTERESTED until the visit is confirmed
APPT=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"${DAY}T16:00:00Z\",\"duration_min\":60}" \
  $BASE/leads/$LEAD/appointments | jq -r '.id')
curl -s -H "$AUTH" "$BASE/agents/$AGENT/calendar?from=${DAY}T00:00:00Z&to=${DAY}T23:59:00Z" \
  | jq '.events[] | {kind, title, status}'

# 7. confirm -> complete -> feedback   (-X PATCH: curl POSTs otherwise).
#    CONFIRMED moves the lead to VISIT_SCHEDULED, COMPLETED to VISITED
curl -s -o /dev/null -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"CONFIRMED"}' $BASE/appointments/$APPT
curl -s -o /dev/null -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"COMPLETED"}' $BASE/appointments/$APPT
curl -s -H "$AUTH" $BASE/leads/$LEAD | jq -r .current_stage          # VISITED
curl -s -o /dev/null -w "feedback: HTTP %{http_code}\n" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"submitted_by":"AGENT","interest_score":4,"objection":"PRICE","close_probability":0.6}' \
  $BASE/appointments/$APPT/feedback

# 8. walk the rest of the funnel to WON
for s in NEGOTIATING WON; do
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

**Rescheduling a confirmed visit** (re-checks overlap). Alone, it becomes
`RESCHEDULED` for the owner to re-confirm; the owner moving their own visit
sends `CONFIRMED` with it:
```bash
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scheduled_at":"2026-09-18T15:00:00Z","status":"CONFIRMED"}' $BASE/appointments/$APPT
```

**Only book slots the agent published** — AI agents always are. For people too,
set `ENFORCE_AVAILABILITY=true` on the API; then `POST /leads/{id}/appointments`
returns **409** for any slot not in `GET /agents/{id}/slots`.

**Instant booking for the bot** — its bookings land `CONFIRMED` (no waiting for
the agent) once it holds `visits:manage`. Do this only when agents have
published their real hours:
```sql
update core.service_account
   set scopes = array_append(scopes, 'visits:manage'), updated_at = now()
 where name = 'ai-agent';
```

**An agent's visits on their phone** — `GET /me/calendar-feed`, then Google
Calendar → *Other calendars → From URL* with `ics_url` (or open `webcal_url` on
an iPhone). Leaked? `POST /me/calendar-feed/rotate`.

**Keep the bot off an agent's personal appointments** — the agent connects
their Google calendar's secret iCal address:
```bash
curl -s -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"ics_url":"https://calendar.google.com/calendar/ical/…/private-…/basic.ics"}' \
  $BASE/me/external-calendar | jq '{last_status, last_error}'
```

**The "to confirm" queue for one agent this week:**
```bash
curl -s -H "$AUTH" "$BASE/agents/$AGENT/calendar?from=2026-09-14T00:00:00Z&to=2026-09-21T00:00:00Z" \
  | jq '[.events[] | select(.kind=="VISIT" and (.status=="PENDING_CONFIRMATION" or .status=="RESCHEDULED"))]'
```

**Board filtered to one agent's live deals:**
```bash
curl -s -H "$AUTH" "$BASE/leads?agent_id=$AGENT&stage=NEGOTIATING" | jq
```

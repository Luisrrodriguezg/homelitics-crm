# Bot & Frontend — quick guide

Short version of how to consume the API from the Telegram bot and from the
frontend. Full reference (every field, every error code): `docs/API_GUIDE.md`.
Why things are built this way: `docs/DECISIONS.md`.

---

## 1. The bot: getting credentials

The bot authenticates as a **service account**, not a human. One account
reaches all agencies.

**One-time setup (project owner does this, not the bot dev):**

1. Confirm migration `007` is applied (already done on the live DB).
2. Supabase dashboard → Authentication → Users → Add user. Email like
   `ai-agent@homelitics.test`, a long random password, tick **Auto Confirm
   User**. Copy the new user's UUID.
3. Run `scripts/provision_ai_agent.sql` with that UUID pasted in. It creates
   the service account and one bot row per agency, and **prints the agency
   ids** at the end.
4. Hand the bot dev, through a secret store (never in the repo or in chat):

   | Value | Where it came from |
   |---|---|
   | `API_BASE` | `https://homelitics-api.onrender.com` |
   | `SUPABASE_URL` | Supabase → Project Settings → API |
   | `SUPABASE_ANON_KEY` | same page — public by design |
   | `BOT_EMAIL` / `BOT_PASSWORD` | step 2 |
   | agency ids | printed by step 3 |

**Rotating the password:** repeat step 2, re-run step 3 with the new UUID
(it re-points the same account), give the bot the new password, delete the
old Auth user.

**If something breaks:** 401 = get a new token. 403 "not linked to an agent"
= step 3 wasn't run for that UUID. 403 "lacks scope" = ask the owner to grant
it (one SQL `UPDATE`, see `API_GUIDE.md` §2d). 429 = bot is writing too fast,
back off.

---

## 2. The bot: how auth works, in one paragraph

The bot trades `BOT_EMAIL`/`BOT_PASSWORD` for a JWT (1h, refresh before
expiry). Every call after that carries two headers:

```
Authorization: Bearer <token>
X-Agency-Id: <agency_uuid>
```

The token proves *who* the bot is; `X-Agency-Id` picks *which agency's bot
row* it's acting as for that request — it can only pick among agencies the
account already has a row in, so it cannot be used to reach someone else's
tenant. A listing belongs to one agency, so the bot must know which agency a
conversation is in (call `GET /listings` per agency once and cache a
listing → agency map).

**Known limitations, worth knowing before this goes further than a class
project:** no database-level tenant isolation (it's all application code —
a bug in a service function is the only thing that could leak across
agencies), one shared credential covers every agency, and the hourly write
cap is a brake against a runaway loop, not a real abuse defense. Details in
`DECISIONS.md` §1 and §17.

---

## 3. The bot: the message flow

Every Telegram message, in order:

1. **Identify the client** — `POST /clients` with `telegram_user_id` always
   set. 201 first time, 200 (same id) after. Name/phone are never
   overwritten on a repeat message.
2. **Open or resume the thread** — `POST /leads` with `client_id` +
   `listing_id` + `source_channel: "TELEGRAM"` + the message text. 201 new
   thread, 200 if one already exists for that (client, listing) pair — this
   is automatic dedup, don't try to replicate it.
3. **Bot replies** — `POST /leads/{id}/interactions`,
   `direction: "OUTBOUND"`. Note: only a human's outbound message/call stops
   the "time to first response" clock. Anything the bot writes is logged but
   doesn't count as the agent answering — that's intentional.
4. **Move the stage when appropriate** — `POST /leads/{id}/transitions`
   with `to_stage`. Stages only move forward in one fixed order
   (`INTERESTED → VISIT_SCHEDULED → VISITED → NEGOTIATING → WON`, or to
   `LOST` from anywhere non-terminal); anything else is rejected.
5. **Book a visit** — `POST /leads/{id}/appointments` with `scheduled_at` +
   `duration_min`. Check `GET /agents/{agent_id}/slots` first so the bot only
   offers real openings (see §4 below for how those slots get there).

**Default bot permissions:** create leads, log interactions, write follow-up
tasks, request visits, register clients. **Not granted by default:** closing
a lead (WON/LOST), managing appointments, writing availability. Ask the
project owner if the bot needs more.

---

## 4. Feeding the calendar

This is done by the agent (or an admin on their behalf), through the
frontend — not the bot. It fills `core.agent_availability` (weekly pattern)
and `core.agent_time_off` (one-off blocks); `GET /agents/{id}/slots` turns
both into the actual bookable grid.

1. **Publish weekly hours**, one call per day/block:
   ```
   POST /agents/{agent_id}/availability
   { "weekday": 0, "start_time": "09:00", "end_time": "13:00" }
   ```
   `weekday`: 0 = Monday … 6 = Sunday. Times are local (`America/Bogota`),
   not UTC. Repeat for each day and time block the agent works.

2. **Block off exceptions** (vacation, an appointment) as needed:
   ```
   POST /agents/{agent_id}/time-off
   { "starts_at": "2026-09-15T14:00:00Z", "ends_at": "2026-09-15T18:00:00Z" }
   ```
   This one **is** UTC — it's a real timestamp, not a weekly rule.

3. **Check the result** — always read the computed grid, never trust the
   raw rules:
   ```
   GET /agents/{agent_id}/slots?from=...&to=...
   ```
   This already subtracts time-off and existing booked visits.

4. **Edit or remove** a rule/block with `PATCH`/`DELETE` on its id — don't
   re-post to "fix" one. Remember `curl -d` sends `POST`; PATCH/DELETE need
   `-X PATCH` / `-X DELETE` explicitly.

5. Bookings aren't rejected for falling outside published hours **unless**
   the API has `ENFORCE_AVAILABILITY=true` set — ask the project owner if
   that should be on.

**Current live DB:** all 48 agents already have default Mon–Fri availability
seeded (2026-09-03 cleanup) — you're editing a real baseline, not starting
from empty.

---

## 5. The frontend

Authenticates as a normal human agent (Supabase JWT) — no `X-Agency-Id`
needed, the agent's own agency scopes everything. Anything outside it is a
404, never a 403.

| Screen | Endpoints |
|---|---|
| Funnel board | `GET /leads` (filter by `stage`, `agent_id`, `listing_id`) |
| Lead detail | `GET /leads/{id}/transitions`, `GET/POST /leads/{id}/interactions`, `GET/POST/PATCH /leads/{id}/tasks` |
| Calendar / visits | `GET/POST /agents/{id}/availability`, `.../time-off`, `GET /agents/{id}/slots`, `POST /leads/{id}/appointments`, `PATCH /appointments/{id}`, `POST /appointments/{id}/feedback` |
| Dashboards | `GET /analytics/north-star`, `/analytics/agent-response-time`, `/analytics/funnel-daily`, `/analytics/listing-performance` — never compute these from raw data client-side |

**Two easy mistakes:** money fields are JSON strings (`"650137717.29"`), not
floats — parse as decimal. Timestamps are UTC ISO-8601 on the wire even
though calendar math happens in `America/Bogota` server-side.

---

Questions or anything unclear → the full reference is `docs/API_GUIDE.md`,
interactive docs are at `/docs` on the running API.

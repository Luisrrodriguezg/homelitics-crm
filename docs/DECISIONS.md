# Decisions

Structural choices and the reasoning behind them. Written down because each one
is the kind of thing a future reader would otherwise "fix".

---

## 1. Authorization lives in the service layer, not in RLS

**Decision.** Every service function takes an `agency_id` and filters on it. No
row-level security policies exist on `core`, `pii`, `events` or `analytics`.

**Why.**

*The transaction pooler makes per-request claims unsafe.* The API holds one
pooled database identity. The usual RLS pattern — `SET LOCAL request.jwt.claims`
at the start of each request — relies on the setting being scoped to a
transaction on a dedicated session. Supabase's transaction pooler (6543)
multiplexes many logical sessions onto few backends, so that setting can outlive
the request that made it and be read by the next one. Silent cross-tenant leakage
is a far worse failure than a missing `WHERE`.

*The analytics views would need policies on every base table.* They are plain
`SECURITY INVOKER` views, so RLS would have to be enforced on `lead`,
`agent`, `listing`, `interaction`, `property_view` and more — and kept correct as
the views change.

*PostgREST cannot reach this data anyway.* Verified against the live database:
`anon` and `authenticated` have **no `USAGE`** on any of our four schemas and
zero table grants. The Supabase REST endpoint therefore cannot read these tables
regardless of RLS. RLS would be defence-in-depth against a threat that has no
path, at the cost of a real one that does.

**Cost, stated plainly.** A service function that forgets its `agency_id` filter
leaks data and nothing else catches it. That is why `tests/test_tenancy.py`
exists and why every read goes through a helper that joins `core.agent`.

**Revisit if** the API moves to per-user database connections, or if something
other than this service starts reading these schemas.

---

## 2. No Alembic

Numbered `.sql` files in `migrations/`, applied with `scripts/apply_migrations.py`
(one transaction per file, against the session pooler) or pasted into the
Supabase SQL editor. Each file is written to be idempotent, so re-running is safe.

The schema was already applied and stable before the API existed, the fix files
are short, and there is one deployment target. Alembic would add an
autogenerate step whose diffs would need reviewing anyway, plus a version table,
to manage two files. `schema-2.sql` remains the single readable source of truth
and is kept in step by hand.

**Revisit if** migrations start arriving faster than one a month, or a second
environment appears.

---

## 3. Appointment overlap is prevented in the API, with a row lock

`schema-2.sql` deliberately cut `EXCLUDE USING gist`: an inline `tstzrange` built
from `(scheduled_at, duration_min)` is not `IMMUTABLE`, so the constraint will
not create.

So `services/appointment.py::_assert_no_overlap` does it. Two locks, because
each covers a case the other misses:

* `pg_advisory_xact_lock(hashtextextended('appt:'||agent_id, 0))` first. A
  `SELECT ... FOR UPDATE` that matches **no** rows locks nothing, so for a
  brand-new slot two concurrent bookers both see "no overlap" and both insert.
  The per-agent, transaction-scoped advisory lock serialises them regardless of
  whether any appointment row exists yet. It is `xact`-scoped, so it releases on
  commit — safe under the transaction pooler (a session-scoped advisory lock
  would not be).
* `SELECT ... FOR UPDATE` on the agent's overlapping rows still matters once
  appointments *do* exist: it stops a concurrent reschedule from moving a row
  out from under this check.

`tests/test_appointment_overlap.py` fires four simultaneous identical bookings
and asserts exactly one `201`.

Intervals are half-open, `[start, end)`, so a visit ending at 11:00 does not
block one starting at 11:00.

---

## 4. Lead dedup is the database's UNIQUE constraint, never Python

`core.lead` has `UNIQUE (client_id, listing_id)`. `POST /leads` uses
`INSERT ... ON CONFLICT DO NOTHING RETURNING id` and, on conflict, selects the
existing row — returning **200** instead of **201**.

Checking "does this lead exist?" in Python first is a race: two concurrent
requests both see nothing, both insert, and one gets an `IntegrityError` 500.
`tests/test_dedup.py` fires six concurrent identical posts and asserts exactly
one `201` and one distinct id.

---

## 5. The stage-sync trigger is guarded (002_fixes.sql)

`core.sync_lead_stage` originally updated `lead.current_stage` unconditionally,
so it wrote in **insert** order rather than `changed_at` order. A backdated or
concurrent transition would leave the cache pointing at an older stage than the
log — and since every funnel metric derives from the log, the two would silently
disagree.

The guard is `where new.changed_at >= (select max(changed_at) ... )`. Because
this is an `AFTER INSERT` trigger the new row is already visible to that
subquery, so the condition is true exactly when `NEW` is the newest transition.
Out-of-order transitions are still **logged** — only the cache update is skipped.

The same statement pins `search_path` to `''`, clearing a Supabase advisor
warning; every reference in the body is schema-qualified as that requires.

Verified against the live database: in-order → cache follows; backdated → cache
holds; newer → cache follows; all four transitions retained in the log.

---

## 6. `agency_id` was added to two analytics views

`analytics.agent_response_time` and `analytics.listing_performance` exposed no
`agency_id`, so neither could be filtered per tenant. The API would have had to
read `core` to build a dashboard — breaking the one rule the analytics schema
exists to enforce. `002_fixes.sql` adds it to both (appended last, because
`CREATE OR REPLACE VIEW` may only add columns at the end).

Two views were also added for North Star metrics that are not derivable from the
original three: `analytics.lead_outcome` (per lead: first response time, reached
visit, has follow-up, lost within 48h) and `analytics.stage_conversion`.

---

## 7. APScheduler in-process, not pg_cron

The inactivity sweep runs on the FastAPI lifespan. It is deliberately **not**
distributed-safe: every replica running it would raise its own duplicate
follow-up task for the same lead. `ENABLE_SCHEDULER` gates it — run it on
exactly one worker.

It is idempotent within a single runner: a lead that already has a `PENDING`
task is skipped, so running twice in an hour does not double up.

Chosen over pg_cron because the logic writes two related rows and belongs with
the rest of the domain code. **Revisit if** the API scales past one replica —
at that point move it to pg_cron or a dedicated worker, not a leader election.

---

## 8. No repository layer

Routers → services → SQLAlchemy. A repository layer between service and ORM
would be indirection with one implementation behind it. The services are already
the seam that would be mocked.

---

## 9. Analytics endpoints use raw SQL

The `analytics.*` objects are plain views with no primary key. Mapping them as
ORM entities buys nothing — there is no identity map to benefit from and no
relationships to traverse. `text()` keeps the query readable and close to the
view definition.

---

## 10. Value sets stay `text` + `CHECK`

Mirrored in `schemas.py` as `Literal`s so bad input is a 422 rather than a 500
from a constraint violation. The database keeps `text` + `CHECK` (not Postgres
enums) so a new value is a plain `ALTER`. The two must be kept in step by hand —
that is the accepted cost of not using enums.

---

## 11. Agent availability is two tables + API slot maths (003_availability.sql)

The original scope cut availability tables and turned HU-02 into *request a
visit → agent confirms*. HU-05 ("an agent publishes when they are reachable")
still needs storage, so `003` adds the minimum: `core.agent_availability`
(weekly recurring blocks, `weekday 0..6` + a validity window) and
`core.agent_time_off` (ad-hoc, half-open `[starts_at, ends_at)`).

Turning rules into bookable slots stays in the API
(`services/availability.compute_slots`): expand the weekly rules over a date
range in `APP_TIMEZONE` (`America/Bogota`), subtract time off and
calendar-blocking appointments, emit a 30-minute grid. It imports `_BLOCKING`
from `services/appointment.py` rather than re-listing appointment statuses — the
overlap check and the slot view must never disagree about what occupies a
calendar.

`request_visit` gains optional enforcement behind `ENFORCE_AVAILABILITY`
(default **false**, so `tests/test_appointment_overlap.py` keeps its meaning).

**Revisit if** availability becomes an external service — the tables are a thin
local cache and `compute_slots` is the only consumer.

---

## 12. Domain events: a transactional outbox, not Kafka (004_events_outbox.sql)

CLAUDE.md cut the event bus. `events.domain_event` is the outbox that keeps the
event *data model* without the infrastructure:

* `services/events.emit(...)` adds the row **to the caller's session** with no
  commit of its own, so an event exists iff the business transaction commits.
  Call sites: `create_or_get_lead` → `lead.created`, `add_transition` →
  `lead.stage_changed`, `request_visit` → `appointment.booked`, the inactivity
  sweep → `lead.went_cold`.
* `jobs.relay_events` runs every `EVENT_RELAY_SECONDS` (same `ENABLE_SCHEDULER`
  gate as the sweep), dispatches unpublished rows to in-process handlers and
  stamps `published_at`. A handler that raises leaves the row unpublished with
  `attempts` bumped — one bad event does not stall the rest. `on_lead_created`
  raises the first-touch follow-up (HU-10 entry point).

**The Realtime grant is surgical.** Decision 1 relies on `authenticated` having
no `USAGE` on any of our schemas. Opening Realtime to the dashboard must not
undo that, so `004` grants exactly: `USAGE` on `events`, `SELECT` on
`domain_event` only, RLS on that one table, one policy
`agency_id = core.current_agency_id()` (a `security definer` function resolving
`auth.uid()` → `core.agent.agency_id`). `core`, `analytics`, `pii` and every
other `events` object stay closed. `scripts/verify_db.py` asserts this is the
only grant. The whole block is guarded behind an `auth` schema check so the same
file applies to the plain-Postgres container in the compose `local` profile.

---

## 13. Views stay plain — measured, not assumed

Story 2 AC6 names materialized views. Kept plain. Server-side execution time
(`EXPLAIN ANALYZE`, so no network / row-transfer noise) of all five
`analytics.*` views on the full ~9.4k-lead / ~372k-view dataset, measured
2026-09-01 with `scripts/measure_views.py`:

| view | p50 | p95 | rows |
|---|---|---|---|
| `funnel_daily` | 41 ms | 42 ms | 8,146 |
| `agent_response_time` | 50 ms | 51 ms | 48 |
| `listing_performance` | 132 ms | 133 ms | 1,200 |
| `lead_outcome` | 105 ms | 125 ms | 9,402 |
| `stage_conversion` | 51 ms | 54 ms | 36 |

All well under a dashboard's tolerance, so a matview + refresh job would be
moving parts with nothing to buy them. **Revisit** the moment one crosses
~500 ms — at that point build the matview for that view only, not all five.
Re-run `scripts/measure_views.py` to check.

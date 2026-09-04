# Ground truth — what is in `homelitics` right now

Loaded **2026-09-01** into `homelitics` (`msmpbounvqtfobcfyffr`) by

```
python seed.py --scale medium --seed 42 --months 12 --now 2026-08-31T00:00:00Z
```

(`medium` = 6 agencies × 8 agents, 1,200 properties, 6,200 clients). It replaced
the earlier `scripts/seed_sql.sql` load described in older versions of this file.
Figures below were measured read-only on **2026-09-03**, after the pytest debris
was purged, so they describe real rows only.

> Both seeders **TRUNCATE every table first**. Re-seeding also wipes
> `core.agent.auth_user_id` (logins) and `core.agent_availability`. Don't, unless
> you mean to re-run `scripts/provision_agent_users.py` and the availability seed.
>
> `seed.py` is reproducible row-for-row for a given `--seed`/`--now`, so these
> counts are exact for this command line.

## Volume — ~460,000 rows

| table | rows |
|---|---|
| `pii.person` | 7,634 |
| `core.agency` | 6 |
| `core.agent` | 48 (6 TEAM_ADMIN, 42 AGENT) |
| `core.owner` | 1,220 |
| `core.client` | 6,386 |
| `core.property` | 1,200 |
| `core.listing` | 1,200 |
| `core.lead` | 9,402 |
| `core.lead_stage_transition` | 25,078 |
| `core.interaction` | 22,495 |
| `core.appointment` | 3,063 |
| `core.visit_feedback` | 3,797 |
| `core.offer` | 2,314 |
| `core.deal` | 551 |
| `core.lead_lost_detail` | 8,502 |
| `core.follow_up_task` | 200 (grows hourly: the pg_cron sweep raises ≈346 on its first run) |
| `core.assignment_audit` | 313 |
| `core.agent_availability` | 480 after the 2026-09-03 seed (Mon–Fri 09–12 + 14–18 for every agent) |
| `events.property_view` | **371,567** |
| `events.domain_event` | 13 (only API-created rows; the seeder does not write the outbox) |

Lead outcomes: **551 WON**, **8,502 LOST**, **349 still open**
(200 INTERESTED, 47 VISIT_SCHEDULED, 63 VISITED, 39 NEGOTIATING).

## Cohorts — identified by measurement, not by a flag

`seed.py` marks slow responders and overpriced listings internally; no flag column
exists in the schema, so recover them from the analytics views the way
`scripts/verify_db.py` does:

```sql
-- slow responders: agents whose median first response exceeds 10 h (5 of 48)
select agent_id
from analytics.lead_outcome
group by agent_id
having percentile_cont(0.5) within group
       (order by extract(epoch from first_response_time)) > 10 * 3600;

-- overpriced: > 15 % above the neighbourhood median asking price per m²
with ppm as (
  select lp.listing_id, p.neighborhood, li.asking_price / p.area_m2 as price_m2
  from analytics.listing_performance lp
  join core.listing li on li.id = lp.listing_id
  join core.property p on p.id = li.property_id),
med as (select neighborhood, percentile_cont(0.5) within group (order by price_m2) mid
        from ppm group by neighborhood)
select listing_id from ppm join med using (neighborhood) where price_m2 > mid * 1.15;
```

## Injected patterns — verified against `analytics.*` (2026-09-03)

| pattern | cohort | control | gap |
|---|---|---|---|
| median time to first response | **29.3h** (5 slow agents, 1,092 leads) | 2.0h (43 agents) | **~14×** |
| lead → visit conversion | 10.8% (slow) | **27.4%** (fast) | −16.6 pts |
| avg views per listing | **326** (overpriced) | 299 (fair) | +9% |
| listings with ≥ 1 win | 24.5% (overpriced) | **34.9%** (fair) | −10 pts |
| never answered | 1,854 leads (19.7%) | — | LOST with `NO_RESPONSE`, zero OUTBOUND |
| missing emails | 888 persons (11.5%) | — | data-quality fixture |
| duplicate clients | 228 name groups | — | same person, reformatted phone (plus genuine homonyms) |

The point of these is that analytics work can be **validated** rather than
assumed: if a dashboard does not show slow agents converting worse, the
dashboard is wrong. `python scripts/verify_db.py` re-derives the first four rows.

## North Star metrics (all agencies, `analytics.lead_outcome`)

| metric | value |
|---|---|
| leads | 9,402 |
| median time to first response | 2.3h |
| % with at least one follow-up | 2.1% (before the sweep started running; rises hourly) |
| lead → visit conversion | 25.5% |
| % lost within 48h | 16.0% |

## Funnel (`analytics.stage_conversion`, all agencies)

| stage | leads reached | conversion from previous |
|---|---|---|
| INTERESTED | 9,402 | — |
| VISIT_SCHEDULED | 3,063 | 32.6% |
| VISITED | 2,393 | 78.1% |
| NEGOTIATING | 1,167 | 48.8% |
| WON | 551 | 47.2% |
| LOST | 8,502 | (off-funnel) |

## Integrity — all zero

| check | result |
|---|---|
| leads whose `current_stage` ≠ latest transition | **0** |
| duplicate `(client_id, listing_id)` | **0** |
| LOST leads with no `lead_lost_detail` | **0** |
| WON leads with no `deal` / deals on non-WON leads | **0** / **0** |
| cross-agency lead/listing ownership | **0** |
| blocking appointments on another agent than the lead's | **0** (1 repaired 2026-09-03) |
| interactions dated before their lead | **0** (14 repaired 2026-09-03) |
| `pytest-*` agencies | **0** (22 purged 2026-09-03) |

Known and accepted: 104 COMPLETED/CANCELLED appointments sit under the agent who
owned the lead *before* a reassignment — that is history, not an error. 19 visits
scheduled between Aug 31 and Sep 3 are still CONFIRMED because the seed clock is
pinned at Aug 31 and nothing has recorded their outcome; that is what a live CRM
looks like, and the reminder/close-out job (Línea 1, task 1.3) is where it gets
handled.

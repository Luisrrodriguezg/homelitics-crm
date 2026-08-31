# Ground truth — SQL-seeded dataset

Loaded **2026-08-31** into `homelitics` (`msmpbounvqtfobcfyffr`) by
[`scripts/seed_sql.sql`](../scripts/seed_sql.sql), which generates everything
server-side through the Supabase connector.

> **This is not `seed.py`'s dataset.** `seed.py --seed 42` produces a different,
> smaller set (1,789 leads / 76,801 views) and its `load()` **TRUNCATEs every
> table first** — running it will destroy the data described here. The seed-42
> figures quoted in `CLAUDE.md` refer to that generator, not this one.
>
> This dataset is also **not** reproducible row-for-row: it uses Postgres
> `random()` across several statements. The *cohorts* are deterministic (fixed
> ordinal rules on immutable ids), so the patterns below reproduce; the exact
> counts will not.

## Volume — ~499,000 rows

| table | rows |
|---|---|
| `pii.person` | 7,429 |
| `core.agency` | 6 |
| `core.agent` | 48 (6 TEAM_ADMIN, 42 AGENT) |
| `core.owner` | 1,200 |
| `core.client` | 6,181 |
| `core.property` | 1,200 |
| `core.listing` | 1,200 |
| `core.lead` | 13,000 |
| `core.lead_stage_transition` | 32,789 |
| `core.interaction` | 34,690 |
| `core.appointment` | 4,607 |
| `core.visit_feedback` | 5,559 |
| `core.offer` | 3,452 |
| `core.deal` | 795 |
| `core.lead_lost_detail` | 9,519 |
| `core.follow_up_task` | 1,879 |
| `core.assignment_audit` | 406 |
| `events.property_view` | **375,281** |

Lead outcomes: **795 WON**, **9,519 LOST**, **2,686 still open**.

## Cohort definitions — stable, re-derivable in any query

Both use `row_number() over (order by id)`, which is deterministic because `id`
is immutable. No flag column was added to the schema.

```sql
-- the 9 slow-responder agents (20%)
select id from (select id, row_number() over (order by id) ord from core.agent) z
where ord % 5 = 0;

-- the 171 overpriced listings (14%), priced +25% over fair value
select id from (select id, row_number() over (order by id) ord from core.listing) z
where ord % 7 = 0;
```

## Injected patterns — verified against `analytics.*`

| pattern | cohort | control | gap |
|---|---|---|---|
| median time to first response | **36.0h** (slow) | 2.4h (fast) | **15×** |
| lead → visit conversion | 15.3% (slow) | **29.1%** (fast) | −13.8 pts |
| avg views per listing | **458** (overpriced) | 289 (fair) | +58% |
| listings with ≥1 win | 18.1% (overpriced) | **53.4%** (fair) | −35 pts |
| never answered | 1,580 leads (12.2%) | — | LOST with `NO_RESPONSE`, zero OUTBOUND |
| missing emails | 604 persons (8.1%) | — | data-quality fixture |
| duplicate clients | 43 name groups | — | same digits, reformatted phone |

The point of these is that analytics work can be **validated** rather than
assumed: if a dashboard does not show slow agents converting worse, the
dashboard is wrong.

## North Star metrics (all agencies)

| metric | value |
|---|---|
| leads | 13,000 |
| median time to first response | 2.8h |
| % with at least one follow-up | 14.5% |
| lead → visit conversion | 26.5% |
| % lost within 48h | 6.7% |

## Funnel

| stage | leads reached | conversion from previous |
|---|---|---|
| INTERESTED | 13,000 | — |
| VISIT_SCHEDULED | 4,303 | 33.1% |
| VISITED | 3,448 | 80.1% |
| NEGOTIATING | 1,724 | 49.9% |
| WON | 795 | 46.1% |
| LOST | 9,519 | (off-funnel) |

## Integrity — all zero

| check | result |
|---|---|
| leads whose `current_stage` ≠ latest transition | **0** |
| duplicate `(client_id, listing_id)` | **0** |
| LOST leads with no `lead_lost_detail` | **0** |
| cross-agency reassignments | **0** |
| `property_view` rows dated in the future | **0** |

The first one matters most: the load ran with `trg_sync_lead_stage` disabled for
speed, then reconciled `current_stage` from the log and re-enabled the trigger.
Zero drift confirms the reconciliation was correct and the cache agrees with the
transition log, which is the invariant every funnel metric depends on.

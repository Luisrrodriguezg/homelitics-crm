#!/usr/bin/env python3
"""
Read-only verification of the live homelitics database.

Two groups of checks:

  STRUCTURE  always runs. Asserts the live schema matches what schema-2.sql
             plus migrations/002_fixes.sql declare. This is the guard that runs
             *before* anything destructive: schema-2.sql opens with
             `drop schema ... cascade`, so if the live DB has drifted, we stop.

  DATA       skipped when the database is empty. Asserts the trigger held
             through the bulk load, the dedup guard is intact, and the ground
             truth the seeder injects is actually measurable.

Usage:
    python scripts/verify_db.py                  # uses DATABASE_URL_MIGRATE
    python scripts/verify_db.py --url postgres://...

Exit code 0 = all checks passed, 1 = at least one failed.
"""
import argparse
import os
import sys

import psycopg2

# ---------------------------------------------------------------- expectations

CORE_TABLES = {
    "agency", "agent", "owner", "client", "property", "listing", "lead",
    "lead_stage", "lead_stage_transition", "lost_reason", "lead_lost_detail",
    "interaction", "appointment", "objection", "visit_feedback",
    "follow_up_task", "assignment_audit", "offer", "deal",
}
ANALYTICS_VIEWS = {
    "funnel_daily", "agent_response_time", "listing_performance",
    "lead_outcome", "stage_conversion",
}
EXPECTED_INDEXES = {
    # from 001_schema.sql
    "idx_lead_agent", "idx_lead_listing", "idx_transition_lead",
    "idx_interaction_lead", "idx_appointment_lead", "idx_appointment_agent",
    "idx_view_listing",
    # from 002_fixes.sql
    "idx_agent_auth_user_id", "idx_agent_agency", "idx_listing_agent",
    "idx_listing_status", "idx_property_owner", "idx_task_agent_due",
}

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def one(cur, sql, args=None):
    cur.execute(sql, args or ())
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- structure

def verify_structure(cur):
    print("\nSTRUCTURE")

    cur.execute("""select table_schema, table_name from information_schema.tables
                   where table_schema in ('pii','core','events','analytics')
                     and table_type = 'BASE TABLE'""")
    tables = {}
    for schema, name in cur.fetchall():
        tables.setdefault(schema, set()).add(name)

    check("pii has exactly {person}", tables.get("pii") == {"person"},
          str(sorted(tables.get("pii", []))))
    check("events has exactly {property_view}", tables.get("events") == {"property_view"},
          str(sorted(tables.get("events", []))))

    core = tables.get("core", set())
    missing, extra = CORE_TABLES - core, core - CORE_TABLES
    check(f"core has the 19 expected tables", not missing and not extra,
          f"missing={sorted(missing)} unexpected={sorted(extra)}" if (missing or extra)
          else "19/19")

    cur.execute("""select table_name from information_schema.tables
                   where table_schema='analytics' and table_type='VIEW'""")
    views = {r[0] for r in cur.fetchall()}
    check("analytics has the 5 expected views", views == ANALYTICS_VIEWS,
          f"missing={sorted(ANALYTICS_VIEWS - views)} unexpected={sorted(views - ANALYTICS_VIEWS)}"
          if views != ANALYTICS_VIEWS else "5/5")

    # every view must expose agency_id, or it cannot be filtered per tenant
    cur.execute("""select table_name from information_schema.columns
                   where table_schema='analytics' and column_name='agency_id'""")
    with_agency = {r[0] for r in cur.fetchall()}
    check("every analytics view exposes agency_id", with_agency == ANALYTICS_VIEWS,
          f"lacking={sorted(ANALYTICS_VIEWS - with_agency)}"
          if with_agency != ANALYTICS_VIEWS else "5/5")

    check("core.agent.auth_user_id exists",
          one(cur, """select count(*) from information_schema.columns
                      where table_schema='core' and table_name='agent'
                        and column_name='auth_user_id'""") == 1)

    cur.execute("""select indexname from pg_indexes
                   where schemaname in ('core','events','pii')
                     and indexname not like '%_pkey'""")
    idx = {r[0] for r in cur.fetchall()}
    missing_idx = EXPECTED_INDEXES - idx
    check("all expected indexes present", not missing_idx,
          f"missing={sorted(missing_idx)}" if missing_idx else f"{len(EXPECTED_INDEXES)}/{len(EXPECTED_INDEXES)}")

    src = one(cur, """select pg_get_functiondef(p.oid) from pg_proc p
                      join pg_namespace n on n.oid = p.pronamespace
                      where n.nspname='core' and p.proname='sync_lead_stage'""") or ""
    check("sync_lead_stage is the GUARDED version", "max(t.changed_at)" in src,
          "unguarded — a backdated transition will corrupt current_stage" if "max(t.changed_at)" not in src else "")
    check("sync_lead_stage has a pinned search_path", "search_path" in src.lower())

    # the reason running without RLS is safe: PostgREST simply cannot reach these schemas
    leaked = one(cur, """select count(*) from information_schema.role_table_grants
                         where grantee in ('anon','authenticated')
                           and table_schema in ('pii','core','events','analytics')""")
    check("anon/authenticated hold no grants on our schemas", leaked == 0,
          f"{leaked} grants found — data is reachable via PostgREST" if leaked else "")


# ---------------------------------------------------------------- data

def verify_data(cur):
    counts = {}
    for t in ("lead", "agent", "lead_stage_transition", "interaction", "appointment"):
        counts[t] = one(cur, f"select count(*) from core.{t}")
    counts["property_view"] = one(cur, "select count(*) from events.property_view")

    print(f"\nROW COUNTS  " + "  ".join(f"{k}={v}" for k, v in counts.items()))

    if counts["lead"] == 0:
        print("\nDATA        skipped — database is empty (run seed.py first)")
        return

    print("\nDATA")

    # The whole schema rests on this: transitions are truth, current_stage is a cache.
    drift = one(cur, """
        select count(*) from core.lead l
        join lateral (
          select t.to_stage from core.lead_stage_transition t
          where t.lead_id = l.id order by t.changed_at desc, t.id desc limit 1
        ) latest on true
        where l.current_stage <> latest.to_stage""")
    check("every current_stage matches its latest transition", drift == 0,
          f"{drift} leads drifted" if drift else "trigger held through bulk load")

    dupes = one(cur, """select count(*) from (
                          select client_id, listing_id from core.lead
                          group by 1,2 having count(*) > 1) d""")
    check("no duplicate (client_id, listing_id)", dupes == 0,
          f"{dupes} duplicate pairs" if dupes else "UNIQUE guard intact")

    orphan = one(cur, """select count(*) from core.lead l
                         where l.current_stage='LOST'
                           and not exists (select 1 from core.lead_lost_detail d
                                           where d.lead_id = l.id)""")
    check("every LOST lead has a lost_detail row", orphan == 0,
          f"{orphan} LOST leads with no reason" if orphan else "")

    # ---- injected ground truth ----
    print("\nGROUND TRUTH  (seed 42)")

    cur.execute("""
        select round(extract(epoch from percentile_cont(0.5) within group
                 (order by o.first_response_time)) / 3600.0, 1) as median_h,
               count(*) as leads,
               round(100.0 * avg(case when o.reached_visit then 1 else 0 end), 1) as visit_pct,
               slow.is_slow
        from analytics.lead_outcome o
        join lateral (
          select (percentile_cont(0.5) within group (order by
                    extract(epoch from x.first_response_time)) > 10*3600) as is_slow
          from analytics.lead_outcome x where x.agent_id = o.agent_id
        ) slow on true
        group by slow.is_slow order by slow.is_slow""")
    rows = {r[3]: r for r in cur.fetchall()}
    if len(rows) == 2:
        fast, slow = rows[False], rows[True]
        check("slow responders are markedly slower",
              slow[0] > fast[0] * 3,
              f"slow median {slow[0]}h vs fast {fast[0]}h")
        check("slowness depresses lead->visit conversion",
              slow[2] < fast[2],
              f"slow {slow[2]}% vs fast {fast[2]}% conversion")
    else:
        check("two responder cohorts detectable", False,
              f"found {len(rows)} cohort(s) — expected fast and slow")

    cur.execute("""
        select round(avg(views), 0), round(100.0*avg(case when won>0 then 1 else 0 end), 1),
               (asking_price / nullif(area_m2, 0)) > pct.cut
        from analytics.listing_performance lp
        join core.listing li on li.id = lp.listing_id
        join core.property p on p.id = li.property_id
        cross join (select percentile_cont(0.85) within group
                      (order by li2.asking_price / nullif(p2.area_m2,0)) as cut
                    from core.listing li2 join core.property p2 on p2.id = li2.property_id) pct
        group by 3 order by 3""")
    rows = cur.fetchall()
    if len(rows) == 2:
        normal, over = rows[0], rows[1]
        check("overpriced listings draw more views",
              over[0] > normal[0], f"{over[0]} vs {normal[0]} avg views")
        check("overpriced listings win less often",
              over[1] < normal[1], f"{over[1]}% vs {normal[1]}% win rate")
    else:
        check("price cohorts detectable", False, f"found {len(rows)} cohort(s)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URI (default: $DATABASE_URL_MIGRATE, then $DATABASE_URL)")
    args = ap.parse_args()

    url = args.url or os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("No connection string. Set DATABASE_URL_MIGRATE (session pooler, port 5432)\n"
                 "in .env, export it, or pass --url. See .env.example.")

    conn = psycopg2.connect(url)
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        verify_structure(cur)
        verify_data(cur)
    conn.close()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILED:")
        for n in failed:
            print(f"  - {n}")
        print("\nDo NOT run schema-2.sql against this database — it starts with\n"
              "`drop schema ... cascade` and would wipe it. Reconcile the drift first.")
        sys.exit(1)
    print("Live schema matches the repo. Safe to proceed.")


if __name__ == "__main__":
    main()

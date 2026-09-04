#!/usr/bin/env python3
"""
Read-only verification of the live homelitics database.

Two groups of checks:

  STRUCTURE  always runs. Asserts the live schema matches what schema-2.sql
             (= migrations 001..005) declares. This is the guard that runs
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
from pathlib import Path

import psycopg2

# ---------------------------------------------------------------- expectations

CORE_TABLES = {
    "agency", "agent", "owner", "client", "property", "listing", "lead",
    "lead_stage", "lead_stage_transition", "lost_reason", "lead_lost_detail",
    "interaction", "appointment", "objection", "visit_feedback",
    "follow_up_task", "assignment_audit", "offer", "deal",
    # from 003_availability.sql
    "agent_availability", "agent_time_off",
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
    # from 003_availability.sql / 004_events_outbox.sql
    "idx_agent_availability_agent", "idx_agent_time_off_agent",
    "idx_domain_event_unpublished",
}


def _load_dotenv() -> None:
    """Load .env from the project root, so these scripts work the way the README
    says they do. Without this they only see variables already exported in the
    shell, which is not how anyone actually runs them."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


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
    check("events has exactly {property_view, domain_event}",
          tables.get("events") == {"property_view", "domain_event"},
          str(sorted(tables.get("events", []))))

    core = tables.get("core", set())
    missing, extra = CORE_TABLES - core, core - CORE_TABLES
    check(f"core has the {len(CORE_TABLES)} expected tables", not missing and not extra,
          f"missing={sorted(missing)} unexpected={sorted(extra)}" if (missing or extra)
          else f"{len(CORE_TABLES)}/{len(CORE_TABLES)}")

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

    # The reason running without RLS is safe: PostgREST simply cannot reach these
    # schemas. The ONE deliberate exception (004_events_outbox.sql) is a SELECT on
    # events.domain_event for `authenticated`, so Realtime can stream it — and that
    # table has RLS + an agency policy. Anything else is a real leak.
    cur.execute("""select table_schema, table_name, privilege_type, grantee
                   from information_schema.role_table_grants
                   where grantee in ('anon','authenticated')
                     and table_schema in ('pii','core','events','analytics')""")
    grants = cur.fetchall()
    allowed = {("events", "domain_event", "SELECT", "authenticated")}
    leaked = [g for g in grants if tuple(g) not in allowed]
    check("no grants on our schemas beyond the Realtime SELECT on domain_event",
          not leaked, f"unexpected grants: {leaked}" if leaked else "")

    rls_on = one(cur, """select relrowsecurity from pg_class c
                         join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname='events' and c.relname='domain_event'""")
    check("events.domain_event has RLS enabled", rls_on is True)

    # 005: the background jobs are SQL functions. On Supabase pg_cron calls them;
    # on the local container app/jobs.py does. The functions must exist either way.
    for schema, fn in (("core", "sweep_inactive_leads"), ("events", "relay_domain_events")):
        n = one(cur, """select count(*) from pg_proc p
                        join pg_namespace n on n.oid = p.pronamespace
                        where n.nspname = %s and p.proname = %s""", (schema, fn))
        check(f"{schema}.{fn}() exists (005)", n == 1,
              "missing — apply migrations/005_cron_jobs.sql" if n != 1 else "")

    if one(cur, "select exists (select 1 from pg_namespace where nspname = 'cron')"):
        cur.execute("""select jobname, schedule, active from cron.job
                       where jobname in ('homelitics_sweep', 'homelitics_relay')""")
        jobs = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        ok = set(jobs) == {"homelitics_sweep", "homelitics_relay"} and all(a for _, a in jobs.values())
        check("pg_cron runs homelitics_sweep + homelitics_relay", ok,
              ", ".join(f"{k}={v[0]!r}" for k, v in sorted(jobs.items())) or "no jobs scheduled")
    else:
        print("  skip  pg_cron not installed here — app/jobs.py drives the job functions")


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

    # "Overpriced" has to be judged WITHIN a neighbourhood. Ranking on raw
    # price/m2 across the whole city just selects the premium neighbourhoods,
    # which says nothing about whether a listing is mispriced for where it is.
    cur.execute("""
        with ppm as (
          select lp.listing_id, lp.views, lp.won, p.neighborhood,
                 li.asking_price / nullif(p.area_m2, 0) as price_m2
          from analytics.listing_performance lp
          join core.listing li on li.id = lp.listing_id
          join core.property p on p.id = li.property_id
        ),
        med as (
          select neighborhood,
                 percentile_cont(0.5) within group (order by price_m2) as mid
          from ppm group by neighborhood
        )
        select round(avg(ppm.views), 0),
               round(100.0*avg(case when ppm.won > 0 then 1 else 0 end), 1),
               (ppm.price_m2 > med.mid * 1.15) as overpriced
        from ppm join med on med.neighborhood = ppm.neighborhood
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

    _load_dotenv()

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

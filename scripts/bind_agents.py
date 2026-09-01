#!/usr/bin/env python3
"""Bind Supabase Auth users to core.agent rows.

The API resolves a token's `sub` claim to an agent via core.agent.auth_user_id.
Seeded agents have that column NULL, so until it is set every authenticated
request gets 403. This script sets it.

By default it picks two useful agents from the same agency:

  * a TEAM_ADMIN  — can reassign leads
  * a plain AGENT — and specifically one of the *slow responders* the seeder
                    injects, so the analytics endpoints show the planted
                    pattern instead of a flat, boring line

Usage
-----
    # see what it would do
    python scripts/bind_agents.py --admin <UUID> --agent <UUID> --dry-run

    # do it
    python scripts/bind_agents.py --admin <UUID> --agent <UUID>

    # target specific agents instead of letting it choose
    python scripts/bind_agents.py --admin <UUID> --agent <UUID> \
        --admin-agent-id <agent uuid> --agent-agent-id <agent uuid>

    # undo
    python scripts/bind_agents.py --unbind-all
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras

def _load_dotenv() -> None:
    """Load .env from the project root, so these scripts work the way the README
    says they do. Without this they only see variables already exported in the
    shell, which is not how anyone actually runs them."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


SLOWEST_IN_AGENCY = """
    select a.id, p.full_name, a.role, a.agency_id, a.auth_user_id,
           extract(epoch from art.median_first_response)/3600.0 as median_h,
           art.leads
    from core.agent a
    join pii.person p on p.id = a.person_id
    left join analytics.agent_response_time art on art.agent_id = a.id
    where a.agency_id = %s and a.active
    order by a.role = 'TEAM_ADMIN' desc, median_h desc nulls last
"""


def _valid_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a UUID")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--admin", type=_valid_uuid, help="Supabase auth user UUID -> TEAM_ADMIN")
    ap.add_argument("--agent", type=_valid_uuid, help="Supabase auth user UUID -> plain AGENT")
    ap.add_argument("--admin-agent-id", type=_valid_uuid, help="Bind --admin to this exact agent")
    ap.add_argument("--agent-agent-id", type=_valid_uuid, help="Bind --agent to this exact agent")
    ap.add_argument("--agency-id", type=_valid_uuid, help="Restrict to one agency")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan, change nothing")
    ap.add_argument("--unbind-all", action="store_true",
                    help="Clear every auth_user_id and exit")
    ap.add_argument("--url", help="Postgres URI (default $DATABASE_URL_MIGRATE)")
    args = ap.parse_args()

    _load_dotenv()

    url = args.url or os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    if not url:
        return _fail("Set DATABASE_URL_MIGRATE (session pooler, 5432) or pass --url.")
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)

    if not args.unbind_all and not (args.admin and args.agent):
        return _fail("Provide --admin and --agent (Supabase Auth → Users → the UUIDs), "
                     "or --unbind-all.")
    if args.admin and args.agent and args.admin == args.agent:
        return _fail("--admin and --agent must be different Supabase users.")

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    if args.unbind_all:
        cur.execute("update core.agent set auth_user_id = null where auth_user_id is not null")
        print(f"cleared auth_user_id on {cur.rowcount} agent(s)")
        conn.commit()
        conn.close()
        return 0

    cur.execute("select count(*) from core.agent")
    if cur.fetchone()[0] == 0:
        conn.close()
        return _fail("No agents exist. Run seed.py first.")

    # Choose an agency: the one with the most leads, so the analytics have substance.
    if args.agency_id:
        agency_id = args.agency_id
    else:
        cur.execute("""
            select a.agency_id, count(l.id) as leads
            from core.agent a left join core.lead l on l.agent_id = a.id
            group by a.agency_id order by leads desc limit 1
        """)
        agency_id = cur.fetchone()[0]

    cur.execute(SLOWEST_IN_AGENCY, (agency_id,))
    candidates = cur.fetchall()

    admins = [r for r in candidates if r["role"] == "TEAM_ADMIN"]
    plains = [r for r in candidates if r["role"] == "AGENT"]
    if not admins or not plains:
        conn.close()
        return _fail(f"Agency {agency_id} needs at least one TEAM_ADMIN and one AGENT.")

    admin_row = _pick(candidates, args.admin_agent_id) or admins[0]
    # plains is already ordered slowest-median-first
    agent_row = _pick(candidates, args.agent_agent_id) or plains[0]

    if admin_row["id"] == agent_row["id"]:
        conn.close()
        return _fail("Chose the same agent twice; pass --admin-agent-id/--agent-agent-id.")

    print(f"agency {agency_id}\n")
    plan = [(args.admin, admin_row), (args.agent, agent_row)]
    for auth_uid, row in plan:
        med = f"{row['median_h']:.1f}h" if row["median_h"] is not None else "no data"
        warn = ""
        if row["auth_user_id"] and str(row["auth_user_id"]) != auth_uid:
            warn = f"   [!] currently bound to {row['auth_user_id']} — will be overwritten"
        print(f"  {auth_uid}")
        print(f"    -> {row['role']:11} {row['full_name']}")
        print(f"       agent_id {row['id']}  median first response {med}"
              f"  ({row['leads'] or 0} leads){warn}")

    if args.dry_run:
        print("\ndry run — nothing written")
        conn.close()
        return 0

    for auth_uid, row in plan:
        # Clear any other agent holding this auth id: the column is UNIQUE.
        cur.execute(
            "update core.agent set auth_user_id = null where auth_user_id = %s and id <> %s",
            (auth_uid, row["id"]),
        )
        cur.execute("update core.agent set auth_user_id = %s where id = %s", (auth_uid, row["id"]))
    conn.commit()
    conn.close()

    print("\nbound. Get a token and check it:")
    print("  curl -s -X POST \"$SUPABASE_URL/auth/v1/token?grant_type=password\" \\")
    print("    -H \"apikey: $SUPABASE_ANON_KEY\" -H 'Content-Type: application/json' \\")
    print("    -d '{\"email\":\"...\",\"password\":\"...\"}' | jq -r .access_token")
    print("  curl -s localhost:8000/me -H \"Authorization: Bearer $TOKEN\"")
    return 0


def _pick(rows, agent_id):
    if not agent_id:
        return None
    for r in rows:
        if str(r["id"]) == agent_id:
            return r
    sys.exit(f"agent {agent_id} is not an active agent in that agency")


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

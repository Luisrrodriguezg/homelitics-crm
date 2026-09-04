#!/usr/bin/env python3
"""Create one Supabase Auth login per real agent and bind it to core.agent.

The API resolves a token's `sub` claim to an agent via core.agent.auth_user_id.
Seeded agents have that column NULL, so until it is set every authenticated
request gets 403. `bind_agents.py` binds two users you created by hand; this
script creates the users too, through the Auth Admin API, so nobody has to
type 48 emails into the dashboard.

For every active agent in a non-`pytest-` agency with no auth user yet:

  1. build a deterministic email from the person's name and agency,
       e.g.  maria.mercedes.londono@cruz-oviedo.homelitics.test
  2. POST /auth/v1/admin/users with the service-role key
     (email_confirm=true, so no mail is ever sent; the domain is fake)
  3. write the returned user id into core.agent.auth_user_id and the email
     into pii.person.email so /me shows it

One shared password for every demo login (DEMO_AGENT_PASSWORD). Emails are
printed; the password never is.

Needs in .env:  SUPABASE_URL (or SUPABASE_PROJECT_REF), SUPABASE_SERVICE_ROLE_KEY,
                DEMO_AGENT_PASSWORD, DATABASE_URL_MIGRATE (session pooler, 5432)

Usage
-----
    python scripts/provision_agent_users.py --dry-run     # show the plan
    python scripts/provision_agent_users.py               # do it (idempotent)
    python scripts/provision_agent_users.py --only-admins # 1 TEAM_ADMIN + 1 AGENT per agency
    python scripts/provision_agent_users.py --agency-id <uuid>

Re-running is safe: bound agents are skipped, an email that already exists in
auth.users is looked up rather than re-created.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
import uuid
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent
EMAIL_DOMAIN = "homelitics.test"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def _slug(text: str, sep: str) -> str:
    """'María Mercedes Londoño' -> 'maria.mercedes.londono' (sep='.')."""
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^a-z0-9]+", ascii_.lower()) if p]
    return sep.join(parts)


def _agency_slug(name: str) -> str:
    # 'Pinto LLC Realty' -> 'pinto-llc'; the trailing 'Realty' is noise on every agency.
    return _slug(re.sub(r"\s+Realty$", "", name, flags=re.I), "-") or "agency"


AGENTS_SQL = """
    select a.id as agent_id, a.role, a.agency_id, g.name as agency,
           p.id as person_id, p.full_name, a.auth_user_id
    from core.agent a
    join core.agency g on g.id = a.agency_id
    join pii.person p  on p.id = a.person_id
    where a.active
      and g.name not like 'pytest-%%'
      and (%(agency_id)s::uuid is null or a.agency_id = %(agency_id)s::uuid)
    order by g.name, a.role = 'TEAM_ADMIN' desc, p.full_name
"""


def _admin_create_user(client: httpx.Client, email: str, password: str, meta: dict) -> str:
    """Returns the auth user id. Raises RuntimeError with the API message otherwise.
    'already registered' is signalled by returning the sentinel ''."""
    r = client.post(
        "/auth/v1/admin/users",
        json={"email": email, "password": password, "email_confirm": True,
              "user_metadata": meta},
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    body = r.text
    if r.status_code == 422 and ("already" in body.lower() or "email_exists" in body):
        return ""
    raise RuntimeError(f"{r.status_code} {body[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--only-admins", action="store_true",
                    help="one TEAM_ADMIN + one AGENT per agency instead of everyone")
    ap.add_argument("--agency-id", type=uuid.UUID, help="restrict to one agency")
    ap.add_argument("--url", help="Postgres URI (default $DATABASE_URL_MIGRATE)")
    args = ap.parse_args()

    _load_dotenv()

    db_url = args.url or os.environ.get("DATABASE_URL_MIGRATE")
    supa_url = os.environ.get("SUPABASE_URL") or (
        f"https://{os.environ['SUPABASE_PROJECT_REF']}.supabase.co"
        if os.environ.get("SUPABASE_PROJECT_REF") else None
    )
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    password = os.environ.get("DEMO_AGENT_PASSWORD")

    missing = [n for n, v in (("DATABASE_URL_MIGRATE", db_url), ("SUPABASE_URL", supa_url),
                              ("SUPABASE_SERVICE_ROLE_KEY", service_key),
                              ("DEMO_AGENT_PASSWORD", password)) if not v]
    if missing and not (args.dry_run and set(missing) <= {"SUPABASE_SERVICE_ROLE_KEY", "DEMO_AGENT_PASSWORD"}):
        return _fail(f"missing in .env: {', '.join(missing)}. "
                     "Service-role key: Supabase → Settings → API. Never commit it.")
    if password and len(password) < 8:
        return _fail("DEMO_AGENT_PASSWORD must be at least 8 characters.")

    conn = psycopg2.connect(db_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(AGENTS_SQL, {"agency_id": str(args.agency_id) if args.agency_id else None})
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return _fail("no active agents found (is the DB seeded?)")

    if args.only_admins:
        picked, seen = [], set()
        for r in rows:
            key = (r["agency_id"], r["role"])
            if key not in seen:
                seen.add(key); picked.append(r)
        rows = picked

    # Deterministic emails; guard against two people slugging identically.
    taken: set[str] = set()
    plan = []
    for r in rows:
        base = f"{_slug(r['full_name'], '.')}@{_agency_slug(r['agency'])}.{EMAIL_DOMAIN}"
        email, n = base, 2
        while email in taken:
            email = base.replace("@", f"-{n}@"); n += 1
        taken.add(email)
        plan.append((r, email))

    todo = [(r, e) for r, e in plan if r["auth_user_id"] is None]
    print(f"{len(plan)} agent(s) selected, {len(plan) - len(todo)} already bound, {len(todo)} to provision\n")
    print(f"  {'email':58} {'role':11} agency")
    for r, e in plan:
        mark = " " if r["auth_user_id"] is None else "✓"
        print(f"{mark} {e:58} {r['role']:11} {r['agency']}")

    if args.dry_run:
        print("\ndry run — nothing written")
        conn.close()
        return 0
    if not todo:
        print("\nnothing to do")
        conn.close()
        return 0

    client = httpx.Client(
        base_url=supa_url, timeout=20,
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
    )
    done = failed = 0
    for r, email in todo:
        try:
            meta = {"agent_id": str(r["agent_id"]), "agency_id": str(r["agency_id"]),
                    "role": r["role"], "full_name": r["full_name"]}
            user_id = _admin_create_user(client, email, password, meta)
            if not user_id:
                # Already in auth.users (a previous partial run). We have DB access: look it up.
                cur.execute("select id from auth.users where lower(email) = lower(%s)", (email,))
                got = cur.fetchone()
                if not got:
                    raise RuntimeError("Auth says the email exists but auth.users has no row")
                user_id = str(got[0])

            # auth_user_id is UNIQUE: clear any other agent holding this id (test debris, reruns).
            cur.execute("update core.agent set auth_user_id = null where auth_user_id = %s and id <> %s",
                        (user_id, r["agent_id"]))
            cur.execute("update core.agent set auth_user_id = %s where id = %s", (user_id, r["agent_id"]))
            cur.execute("update pii.person set email = %s, updated_at = now() where id = %s",
                        (email, r["person_id"]))
            conn.commit()
            done += 1
            print(f"  bound  {email}")
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            conn.rollback()
            failed += 1
            print(f"  FAIL   {email}: {exc}", file=sys.stderr)

    conn.close()
    print(f"\n{done} bound, {failed} failed. Password is DEMO_AGENT_PASSWORD from .env (not printed).")
    print("Check one:")
    print('  curl -s -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \\')
    print('    -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \\')
    print(f'    -d \'{{"email":"{todo[0][1]}","password":"..."}}\' | jq -r .access_token')
    return 1 if failed else 0


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

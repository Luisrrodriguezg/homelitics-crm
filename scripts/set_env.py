#!/usr/bin/env python3
"""Fill the two connection strings in .env, interactively and safely.

Why this exists rather than "just edit the file":

  * The password is read with getpass -- never echoed, never in your shell
    history, never in a scrollback someone could screenshot.
  * It is percent-encoded before going into the URI. A password containing
    @ : / ? # or % silently corrupts a connection string, and the error you
    get back ("could not translate host name") points at the wrong thing.
    This is the single most common way this step goes wrong.
  * The two URLs need different schemes and ports; this gets both right.

Usage:
    python scripts/set_env.py            # prompt for host + password
    python scripts/set_env.py --test     # also try connecting
    python scripts/set_env.py --show     # print .env with the password masked
"""
import argparse
import getpass
import re
import sys
from pathlib import Path
from urllib.parse import quote

ENV = Path(__file__).resolve().parent.parent / ".env"
PROJECT_REF = "msmpbounvqtfobcfyffr"


def mask(url: str) -> str:
    return re.sub(r"(//[^:]+:)[^@]+(@)", r"\1********\2", url)


def read_env() -> list[str]:
    if not ENV.exists():
        sys.exit(f"No .env at {ENV}\nCopy it from .env.example first:\n"
                 f"    cp .env.example .env")
    return ENV.read_text().splitlines()


def set_key(lines: list[str], key: str, value: str) -> list[str]:
    out, found = [], False
    for ln in lines:
        if re.match(rf"^{re.escape(key)}=", ln):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    return out


def show():
    for ln in read_env():
        if ln.startswith(("DATABASE_URL=", "DATABASE_URL_MIGRATE=")):
            k, _, v = ln.partition("=")
            print(f"  {k}={mask(v)}")
        elif ln.startswith("SUPABASE_JWT_SECRET="):
            v = ln.partition("=")[2]
            print(f"  SUPABASE_JWT_SECRET={'(blank - correct for this project)' if not v else '********'}")
        elif "=" in ln and not ln.startswith("#") and ln.strip():
            print(f"  {ln}")


def test_connection():
    """Try both URLs. Reports which pooler works, without printing secrets."""
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 not installed. Run:  .venv/bin/pip install -r requirements.txt")

    env = {}
    for ln in read_env():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, _, v = ln.partition("=")
            env[k.strip()] = v.strip()

    ok = True
    for key in ("DATABASE_URL_MIGRATE", "DATABASE_URL"):
        url = env.get(key, "")
        if not url or "[[" in url:
            print(f"  FAIL  {key}  still has a [[placeholder]]")
            ok = False
            continue
        # psycopg2 is sync and does not understand the +asyncpg driver marker
        probe = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        try:
            conn = psycopg2.connect(probe, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("select current_database(), inet_server_port(), count(*) from core.lead")
            db, port, leads = cur.fetchone()
            conn.close()
            print(f"  PASS  {key}  connected  db={db} port={port} leads={leads}")
        except Exception as exc:
            first = str(exc).strip().splitlines()[0]
            print(f"  FAIL  {key}  {first}")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="only test what is already in .env")
    ap.add_argument("--show", action="store_true", help="print .env with secrets masked")
    args = ap.parse_args()

    if args.show:
        show()
        return 0
    if args.test:
        return 0 if test_connection() else 1

    lines = read_env()

    print("Supabase dashboard -> your project -> Settings -> Database -> Connection string")
    print("Both pooler tabs show the SAME host. Copy just the host, e.g.")
    print("    aws-1-us-west-2.pooler.supabase.com\n")
    host = input("Pooler host: ").strip()
    if not host:
        sys.exit("no host given")
    host = host.split("@")[-1].split(":")[0].strip("/")   # tolerate a pasted full URI
    if not host.endswith("pooler.supabase.com"):
        print(f"\n  warning: '{host}' does not look like a pooler host.")
        print("  The Direct-connection host (db.<ref>.supabase.co) is IPv6-only")
        print("  on the free tier and will not work.")
        if input("  continue anyway? [y/N] ").lower() != "y":
            sys.exit("aborted")

    print("\nDatabase password. Not the anon key, not your Supabase login.")
    print("If you never saved it: Settings -> Database -> Reset database password.")
    print("(Input is hidden.)")
    pw1 = getpass.getpass("  Password: ")
    pw2 = getpass.getpass("  Again:    ")
    if not pw1:
        sys.exit("empty password")
    if pw1 != pw2:
        sys.exit("passwords did not match")

    enc = quote(pw1, safe="")
    if enc != pw1:
        print("\n  note: password contained URI-special characters; percent-encoded.")

    user = f"postgres.{PROJECT_REF}"
    lines = set_key(lines, "DATABASE_URL",
                    f"postgresql+asyncpg://{user}:{enc}@{host}:6543/postgres")
    lines = set_key(lines, "DATABASE_URL_MIGRATE",
                    f"postgresql://{user}:{enc}@{host}:5432/postgres")

    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)
    print(f"\nwrote {ENV} (mode 600)\n")
    show()

    print("\ntesting both poolers ...")
    return 0 if test_connection() else 1


if __name__ == "__main__":
    sys.exit(main())

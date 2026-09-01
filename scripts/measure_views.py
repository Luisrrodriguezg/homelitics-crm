#!/usr/bin/env python3
"""p95 latency of the five analytics views on the live dataset.

Read-only. Backs the "views stay plain" decision (docs/DECISIONS.md §13) with a
number instead of an assumption. Runs each view a few times and reports p95.

    python scripts/measure_views.py
"""
from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = [
    "funnel_daily",
    "agent_response_time",
    "listing_performance",
    "lead_outcome",
    "stage_conversion",
]
RUNS = 8


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:
        pass
    import psycopg2

    url = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True, autocommit=True)
    print(f"{'view':24}  {'p50 ms':>8}  {'p95 ms':>8}  rows")
    for v in VIEWS:
        times = []
        rows = 0
        with conn.cursor() as cur:
            for _ in range(RUNS):
                # EXPLAIN ANALYZE: server-side execution time only, no network /
                # row transfer, which is what "is the view slow" actually means.
                cur.execute(f"explain (analyze, timing off, format json) "
                            f"select * from analytics.{v}")
                plan = cur.fetchone()[0][0]
                times.append(plan["Execution Time"])
                rows = plan["Plan"].get("Actual Rows", 0)
        times.sort()
        p50 = statistics.median(times)
        p95 = times[max(0, round(0.95 * RUNS) - 1)]
        print(f"analytics.{v:14}  {p50:8.1f}  {p95:8.1f}  {rows}")
    conn.close()


if __name__ == "__main__":
    main()

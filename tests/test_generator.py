"""Generator invariants — no database.

`generate()` builds tuples in memory, so these run without DATABASE_URL and are
not gated by the live-DB skip in conftest. They pin the three properties the
seed rewrite (Phase 1) was supposed to fix: reproducibility, translation
invariance of `--now`, and the two injected signals AC2/AC3 name.
"""
from datetime import datetime, timedelta, timezone

import pytest

from seed import SCALES, generate

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)
CFG = SCALES["tiny"]


def _gen(now=NOW, seed=42, months=2):
    return generate(CFG, seed, months, now)


def test_same_seed_is_byte_identical():
    assert _gen() == _gen()


def test_now_shifts_the_simulation_as_a_block():
    """`--now` moves the whole simulation by one delta. The seed-fixed dimension
    tables (agents, clients, properties, listings) are identical; the funnel is
    views-driven so its volume drifts a little as month/weekday seasonality lands
    on different calendar dates. A 28-day (4-week) shift keeps weekdays aligned
    and holds that drift small."""
    a = _gen(now=NOW)
    b = _gen(now=NOW + timedelta(days=28))

    for key in ("agents", "clients", "properties", "listings"):
        assert len(a[key]) == len(b[key]), key

    for key in ("leads", "views", "transitions"):
        na, nb = len(a[key]), len(b[key])
        assert abs(na - nb) / na < 0.20, (key, na, nb)

    # what "as a block" means: one constant shift across the seed-fixed rows
    shifts = {lb[7] - la[7] for la, lb in zip(a["listings"], b["listings"])}
    assert shifts == {timedelta(days=28)}


@pytest.mark.parametrize("seed", [42, 7])
def test_rent_is_a_small_fraction_of_sale_per_m2(seed):
    d = _gen(seed=seed)
    area = {p[0]: float(p[6]) for p in d["properties"]}
    per_m2 = {"SALE": [], "RENT": []}
    for _id, prop_id, _agent, op, price, *_ in d["listings"]:
        per_m2[op].append(float(price) / area[prop_id])

    ratio = (sum(per_m2["RENT"]) / len(per_m2["RENT"])) / (
        sum(per_m2["SALE"]) / len(per_m2["SALE"])
    )
    assert 0.003 < ratio < 0.007, ratio


def test_weekend_views_exceed_weekday_views():
    d = _gen()
    by_dow = [0] * 7
    for _id, _lid, _cid, _sid, ts in d["views"]:
        by_dow[ts.weekday()] += 1
    weekday_avg = sum(by_dow[0:5]) / 5
    weekend_avg = sum(by_dow[5:7]) / 2
    assert weekend_avg > weekday_avg, by_dow

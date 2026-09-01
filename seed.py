#!/usr/bin/env python3
"""
Deterministic synthetic data generator for the Real Estate CRM MVP (HT-02).

Two phases: generate() builds tuples in memory with no database at all,
load() pushes them. That is what makes the simulation testable without a
connection.

WARNING: load() TRUNCATEs every table before inserting.

Usage:
    # SESSION pooler, port 5432 -- not the 6543 transaction pooler the API uses.
    export DATABASE_URL_MIGRATE='postgresql://postgres.<ref>:<pw>@...:5432/postgres'
    python seed.py --scale medium --seed 42 --months 12 --now 2026-08-31T00:00:00Z

    # No database: just prints the summary, useful for iterating fast.
    python seed.py --scale medium --seed 42 --dry-run

WHAT THIS REPLACES, AND WHY
----------------------------
* **Reproducibility was broken.** The old generator called `uuid.uuid4()`, which
  reads os.urandom and ignores random.seed(). `--seed 42` reproduced the shape
  of a run but never its content. Every id here comes from the seeded RNG.
* **Geography was fictional** (`Central City`, 12 invented neighborhoods). Now
  a hand-curated Medellín metro area (`geo_medellin.py`): real barrios, real
  municipalities, COP/m² by estrato tier.
* **Rent was priced like sale** (2,095 vs 2,030 COP/m² in the live data --
  effectively the same rate). Rent is now `fair_price x U(0.4%, 0.6%)`.
* **Views had no seasonality** -- one Gaussian total sprinkled uniformly over
  the window. Now a Poisson draw per listing per day, with weekday and month
  multipliers and a post-publish decay curve.
* **Leads were `random.choice(listings)`**, independent of the view stream.
  Now a view converts to a contact with a price/attractiveness-conditioned
  probability; a contact on a new (client, listing) pair opens a lead, a
  contact on an existing pair is a duplicate submission (HU-01 CA3 fixture).
* **The funnel was advance-or-die.** Now a Markov chain with an explicit
  stall state, so a fraction of leads are genuinely still in flight on the
  simulation's last day, and the advance probability is an explicit logit in
  agent responsiveness, price-vs-fair-value and property attractiveness.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from faker import Faker

import geo_medellin as geo

# ----------------------------------------------------------------------
# Scale. `properties` and `clients` are inputs; the LEAD count is an OUTPUT,
# because leads now descend from the simulated view stream instead of being
# drawn to a target count.
# ----------------------------------------------------------------------
SCALES = {
    "tiny":   dict(agencies=2, agents_per_agency=4,  properties=40,   clients=200),
    "small":  dict(agencies=3, agents_per_agency=6,  properties=250,  clients=1300),
    "medium": dict(agencies=6, agents_per_agency=8,  properties=1200, clients=6200),
    "large":  dict(agencies=8, agents_per_agency=10, properties=2500, clients=13000),
}

# --- injected cohorts (AC4) ---
SLOW_AGENT_SHARE       = 0.20
OVERPRICED_SHARE       = 0.14
OVERPRICED_MULTIPLIER  = 1.25
ABANDONED_SHARE        = 0.20     # AC4: "~20% abandoned leads"
DUPLICATE_CLIENT_SHARE = 0.03
MISSING_EMAIL_SHARE    = 0.08

# --- seasonality (AC3) ---
WEEKLY = {0: 1.10, 1: 1.00, 2: 0.98, 3: 0.98, 4: 0.85, 5: 1.35, 6: 1.30}   # Mon..Sun
MONTHLY = {1: 0.70, 2: 1.00, 3: 1.15, 4: 1.18, 5: 1.15, 6: 1.12,
           7: 1.00, 8: 1.05, 9: 1.12, 10: 1.12, 11: 1.05, 12: 0.70}

STAGES = ["INTERESTED", "VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON"]
LOST_REASONS = ["PRICE", "LOCATION", "BOUGHT_ELSEWHERE", "FINANCING", "OTHER"]
DWELL_H = {"INTERESTED": 48, "VISIT_SCHEDULED": 72, "VISITED": 96, "NEGOTIATING": 120}

# Markov chain per stage: intercept of the advance logit, and the chance of
# stalling (neither advancing nor dying) for one more dwell period.
ADVANCE_B0 = {"INTERESTED": -0.25, "VISIT_SCHEDULED": 1.45,
              "VISITED": 0.05, "NEGOTIATING": -0.10}
STALL_P    = {"INTERESTED": 0.18, "VISIT_SCHEDULED": 0.10,
              "VISITED": 0.15, "NEGOTIATING": 0.20}
MAX_STALLS = 3

# Conditioning weights (AC3: agent responsiveness, price-vs-zone-median,
# property attributes). Sign is the whole point of each one.
B_SLOW  = -1.05
B_PRICE = 2.20      # multiplies -log(price_ratio); overpriced -> lower advance
B_ATTR  = 0.55

# Contact-probability conditioning (view -> contact). Overpriced listings get
# MORE views (see daily_lambda) but a LOWER contact rate per view -- browsed,
# not contacted, which is the actual HU-16 pattern.
CONTACT_B0     = -1.65
CONTACT_B_PRICE = 1.10
CONTACT_B_ATTR  = 0.40

# AC4's missing pattern: the best-converting segment. 2-3 bed apartments in a
# tier-4 barrio, priced at or below the zone's fair value.
BEST_SEGMENT = dict(property_type="APARTMENT", tier=4, bedrooms=(2, 3))
B_BEST_SEGMENT = 0.60


# ----------------------------------------------------------------------
# Primitives
# ----------------------------------------------------------------------
def uid(rng) -> str:
    """A UUID4 drawn from the seeded RNG -- uuid.uuid4() reads os.urandom and
    ignores the seed, which is why the old generator was never reproducible."""
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def poisson(rng, lam: float) -> int:
    """Knuth's algorithm. Normal approximation above 30 to keep the loop
    bounded; daily view rates in this simulation never approach that."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(rng.gauss(lam, math.sqrt(lam)) + 0.5))
    target, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= target:
            return k
        k += 1


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def lognormal_hours(rng, median_h: float, sigma: float = 0.8) -> float:
    return rng.lognormvariate(math.log(median_h), sigma)


def time_of_day(rng) -> timedelta:
    """Property browsing clusters in the evening."""
    hour = min(23, max(6, int(rng.gauss(19, 4))))
    return timedelta(hours=hour, minutes=rng.randrange(60), seconds=rng.randrange(60))


def attractiveness(ptype: str, area: float, bedrooms: int, parking: int,
                   year_built: int, ref_year: int) -> float:
    """Roughly -1..1. How desirable the unit is, independent of price."""
    lo, hi = geo.AREA_RANGE[ptype]
    mid, half = (lo + hi) / 2, (hi - lo) / 2
    size_z = max(-1.0, min(1.0, (area - mid) / half))
    age_z = 1.0 - 2.0 * min(ref_year - year_built, 50) / 50.0
    park_z = min(parking, 2) - 1.0
    bed_z = max(-1.0, min(1.0, (bedrooms - 2) / 2.0))
    return 0.35 * size_z + 0.30 * age_z + 0.20 * park_z + 0.15 * bed_z


def is_best_segment(ptype: str, tier: int, bedrooms: int, price_ratio: float) -> bool:
    return (ptype == BEST_SEGMENT["property_type"] and tier == BEST_SEGMENT["tier"]
            and bedrooms in BEST_SEGMENT["bedrooms"] and price_ratio <= 1.0)


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------
def generate(cfg: dict, seed: int, months: int, now: datetime) -> dict:
    import random
    rng = random.Random(seed)
    sim_start = now - timedelta(days=30 * months)

    # A second, independently seeded stream for names/emails/companies. Two
    # deterministic RNGs under one seed are still fully reproducible -- what
    # broke reproducibility before was uuid.uuid4() reading os.urandom, not
    # having more than one seeded source.
    fake = Faker("es_CO")
    Faker.seed(seed)

    persons, agencies, agents, owners, clients = [], [], [], [], []
    properties, listings = [], []
    tasks, audits = [], []

    slow_agents, overpriced_listings, best_segment_listings = set(), set(), set()

    def new_person(email_ok=True, national_id=False):
        pid = uid(rng)
        persons.append((
            pid, fake.name(),
            (fake.email() if (email_ok and rng.random() > MISSING_EMAIL_SHARE) else None),
            geo.phone(rng),
            (fake.numerify("##########") if national_id else None),
        ))
        return pid

    # --- org ---
    for _ in range(cfg["agencies"]):
        aid = uid(rng)
        agencies.append((aid, f"{fake.company()} Realty"))
        for j in range(cfg["agents_per_agency"]):
            agid = uid(rng)
            agents.append((agid, new_person(), aid, "TEAM_ADMIN" if j == 0 else "AGENT", True))
            if rng.random() < SLOW_AGENT_SHARE:
                slow_agents.add(agid)

    agent_ids = [a[0] for a in agents]

    # --- inventory ---
    # listing_meta holds everything the funnel simulation needs per listing,
    # keyed by listing id, without re-deriving it from the raw tuples later.
    listing_meta: dict[str, dict] = {}
    ref_year = now.year

    for _ in range(cfg["properties"]):
        oid = uid(rng)
        owners.append((oid, new_person(national_id=True)))

        ptype = rng.choices(["APARTMENT", "HOUSE", "STUDIO", "COUNTRY_HOUSE"],
                            weights=[52, 26, 14, 8])[0]
        zone = geo.pick_zone(rng, ptype)
        lo, hi = geo.AREA_RANGE[ptype]
        area = round(rng.uniform(lo, hi), 1)
        bedrooms = 0 if ptype == "STUDIO" else rng.randint(1, 5)
        bathrooms = max(1, bedrooms - rng.randint(0, 1)) if bedrooms else 1
        parking = rng.randint(0, 3) if ptype != "STUDIO" else rng.randint(0, 1)
        year_built = rng.randint(1975, 2024)
        hoa = round(rng.uniform(150_000, 650_000), -3) if ptype in ("APARTMENT", "STUDIO") else None

        prid = uid(rng)
        properties.append((
            prid, oid, ptype, zone.city, zone.barrio, geo.street_address(rng, zone),
            area, bedrooms, bathrooms, parking, year_built, hoa,
        ))

        fair_sale = zone.cop_per_m2 * float(area) * geo.TYPE_FACTOR[ptype] * rng.gauss(1.0, 0.08)
        sale_share = geo.SALE_SHARE_BY_TIER[zone.tier]
        op = rng.choices(["SALE", "RENT"], weights=[sale_share, 1 - sale_share])[0]

        over = rng.random() < OVERPRICED_SHARE
        multiplier = OVERPRICED_MULTIPLIER if over else 1.0

        if op == "SALE":
            price = fair_sale * multiplier
        else:
            yield_rate = rng.uniform(*geo.RENT_YIELD_RANGE)
            price = fair_sale * yield_rate * multiplier   # AC2: rent stays a fraction of the SAME fair value

        price = round(price, 2)
        min_accept = round(price * rng.uniform(0.85, 0.95), 2)
        published = sim_start + timedelta(days=rng.uniform(0, max(1, 30 * months - 20)))
        agent_id = rng.choice(agent_ids)

        lid = uid(rng)
        listings.append((lid, prid, agent_id, op, price, min_accept, "ACTIVE", published))

        if over:
            overpriced_listings.add(lid)

        attr = attractiveness(ptype, float(area), bedrooms, parking, year_built, ref_year)
        price_ratio = multiplier * rng.gauss(1.0, 0.03)   # AC4's "priced at/below zone median" test
        best = is_best_segment(ptype, zone.tier, bedrooms, price_ratio)
        if best:
            best_segment_listings.add(lid)

        listing_meta[lid] = dict(
            agent_id=agent_id, operation_type=op, asking_price=price,
            min_acceptable_price=min_accept, published_at=published,
            property_type=ptype, tier=zone.tier, bedrooms=bedrooms,
            overpriced=over, price_ratio=price_ratio, attractiveness=attr,
            best_segment=best,
        )

    # --- clients (with deliberate duplicates, AC5) ---
    for _ in range(cfg["clients"]):
        clients.append((uid(rng), new_person()))
    dup_sample = rng.sample(clients, int(len(clients) * DUPLICATE_CLIENT_SHARE))
    persons_by_id = {p[0]: p for p in persons}
    for _base_cid, base_pid in dup_sample:
        p = persons_by_id[base_pid]
        dup_pid = uid(rng)
        dup_phone = geo.phone_variant(rng, p[3])
        persons.append((dup_pid, p[1], None, dup_phone, None))
        clients.append((uid(rng), dup_pid))

    client_ids = [c[0] for c in clients]

    funnel = _simulate_funnel(
        rng=rng, now=now, sim_start=sim_start, listings=listings,
        listing_meta=listing_meta, client_ids=client_ids, slow_agents=slow_agents,
    )

    # --- light extras: follow-up tasks & a few in-agency reassignments ---
    agents_by_agency: dict[str, list[str]] = {}
    agency_of: dict[str, str] = {}
    for a in agents:
        agents_by_agency.setdefault(a[2], []).append(a[0])
        agency_of[a[0]] = a[2]

    leads_out = funnel["leads"]
    if leads_out:
        for (lead_id, _c, _l, agent_id, _ch, created, _stage) in rng.sample(
            leads_out, min(200, len(leads_out))
        ):
            tasks.append((uid(rng), lead_id, agent_id,
                          created + timedelta(days=rng.randint(1, 14)),
                          "Follow up with client", rng.choice(["PENDING", "DONE"])))

        for idx in rng.sample(range(len(leads_out)), max(1, len(leads_out) // 30)):
            lead_id, cid, lid, agent_id, ch, created, stage = leads_out[idx]
            pool = [x for x in agents_by_agency[agency_of[agent_id]] if x != agent_id]
            if not pool:
                continue
            other = rng.choice(pool)
            audits.append((uid(rng), lead_id, agent_id, other, other, now))
            leads_out[idx] = (lead_id, cid, lid, other, ch, created, stage)

    return dict(
        persons=persons, agencies=agencies, agents=agents, owners=owners,
        clients=clients, properties=properties, listings=listings,
        leads=leads_out, transitions=funnel["transitions"],
        interactions=funnel["interactions"], appointments=funnel["appointments"],
        feedbacks=funnel["feedbacks"], offers=funnel["offers"], deals=funnel["deals"],
        lost_details=funnel["lost_details"], tasks=tasks, audits=audits,
        views=funnel["views"], slow_agents=slow_agents,
        overpriced=overpriced_listings, best_segment=best_segment_listings,
        duplicate_pairs=funnel["duplicate_pairs"],
        stats=funnel["stats"],
    )


# Calibrated so `--scale medium --months 12` lands close to the volume of the
# dataset it replaces (~13k leads / ~375k views): a --dry-run at the raw rates
# below produced 142,854 views / 3,423 leads, short by roughly 2.6x on views
# and needing about 1.5x more contacts per view on top of that.
VIEW_SCALE = 2.6


def _base_daily_rate(tier: int, ptype: str) -> float:
    """Expected daily views for an average (not overpriced, midlife) listing."""
    tier_rate = {1: 0.55, 2: 0.75, 3: 1.00, 4: 1.35, 5: 1.75}[tier]
    type_rate = {"APARTMENT": 1.00, "HOUSE": 0.85, "STUDIO": 0.70, "COUNTRY_HOUSE": 0.55}[ptype]
    return VIEW_SCALE * tier_rate * type_rate


def _walk_lead(rng, now, lead_id, agent_id, slow, meta, created, resp_h_median,
              transitions, interactions, appointments, feedbacks, offers, deals,
              lost_details, used_slots) -> str:
    """Advance one lead from INTERESTED to its final stage. Mutates the shared
    row lists in place; returns the stage the lead ends the simulation in.

    Three exits before the real Markov walk even starts: abandonment (AC4's
    ~20% cohort -- LOST with NO_RESPONSE and zero OUTBOUND interactions), and
    two "still in flight" cases (the abandonment delay, or the agent's reply,
    would land after `now`) that are what let leads be genuinely open on the
    simulation's last day rather than every lead reaching a terminal state.
    """
    if rng.random() < ABANDONED_SHARE:
        lost_at = created + timedelta(days=rng.uniform(3, 10))
        if lost_at >= now:
            return "INTERESTED"
        transitions.append((uid(rng), lead_id, "INTERESTED", "LOST", None, lost_at))
        lost_details.append((lead_id, "NO_RESPONSE", None))
        return "LOST"

    resp_h = lognormal_hours(rng, resp_h_median)
    resp_at = created + timedelta(hours=resp_h)
    if resp_at >= now:
        return "INTERESTED"

    interactions.append((uid(rng), lead_id, "OUTBOUND", "IN_APP", "MESSAGE",
                         "Gracias por escribir. ¿Cuándo te gustaría visitarlo?",
                         resp_at, agent_id))

    t, stage_idx, stalls = resp_at, 0, 0
    appt_id, last_offer_idx = None, None
    price_term = math.log(max(meta["price_ratio"], 0.5))

    while stage_idx < len(STAGES) - 1:
        cur = STAGES[stage_idx]
        t = t + timedelta(hours=lognormal_hours(rng, DWELL_H[cur]))
        if t >= now:
            return cur

        if stalls < MAX_STALLS and rng.random() < STALL_P[cur]:
            stalls += 1
            continue   # one more dwell period at the same stage, no transition

        logit = (ADVANCE_B0[cur] + (B_SLOW if slow else 0.0)
                 - B_PRICE * price_term + B_ATTR * meta["attractiveness"])
        if meta["best_segment"]:
            logit += B_BEST_SEGMENT

        if rng.random() >= sigmoid(logit):
            transitions.append((uid(rng), lead_id, cur, "LOST", agent_id, t))
            reason = ("PRICE" if meta["overpriced"] and rng.random() < 0.6
                      else rng.choice(LOST_REASONS))
            lost_details.append((lead_id, reason, None))
            return "LOST"

        nxt = STAGES[stage_idx + 1]
        transitions.append((uid(rng), lead_id, cur, nxt, agent_id, t))

        if nxt == "VISIT_SCHEDULED":
            slot = (t + timedelta(days=rng.randint(1, 7))).replace(
                hour=rng.randint(8, 18), minute=0, second=0, microsecond=0)
            while (agent_id, slot.isoformat()) in used_slots:
                slot += timedelta(hours=1)
            used_slots.add((agent_id, slot.isoformat()))
            appt_id = uid(rng)
            appointments.append([appt_id, lead_id, agent_id, slot, 60, None])

        elif nxt == "VISITED" and appt_id:
            feedbacks.append((uid(rng), appt_id, "AGENT", rng.randint(3, 5),
                              None, round(rng.uniform(0.3, 0.9), 2), None))
            if rng.random() < 0.6:
                obj = "PRICE" if meta["overpriced"] and rng.random() < 0.5 else None
                feedbacks.append((uid(rng), appt_id, "CLIENT", rng.randint(2, 5), obj, None, None))

        elif nxt == "NEGOTIATING":
            for k in range(rng.randint(1, 3)):
                offers.append([uid(rng), lead_id,
                              round(rng.uniform(meta["min_acceptable_price"], meta["asking_price"]), 2),
                              "CLIENT" if k % 2 == 0 else "AGENT", "COUNTERED", t + timedelta(days=k)])
                last_offer_idx = len(offers) - 1

        elif nxt == "WON":
            amount = round(rng.uniform(meta["min_acceptable_price"], meta["asking_price"] * 0.98), 2)
            commission = (round(amount * 0.03, 2) if meta["operation_type"] == "SALE"
                         else round(amount, 2))
            deals.append((lead_id, amount, commission, t,
                         t.date() if meta["operation_type"] == "RENT" else None,
                         12 if meta["operation_type"] == "RENT" else None))
            if last_offer_idx is not None:
                offers[last_offer_idx][4] = "ACCEPTED"

        stage_idx += 1

    return "WON"


def _simulate_funnel(rng, now, sim_start, listings, listing_meta, client_ids, slow_agents) -> dict:
    """The temporal chain (AC3): daily Poisson views -> price/attractiveness
    conditioned contacts -> a Markov walk over the funnel stages. This is
    where leads come from now, instead of `random.choice(listings)`."""
    leads, transitions, interactions, appointments = [], [], [], []
    feedbacks, offers, deals, lost_details, views_rows = [], [], [], [], []
    lead_by_pair: dict[tuple[str, str], str] = {}
    duplicate_pairs: set[tuple[str, str]] = set()
    used_slots: set[tuple[str, str]] = set()
    stats = dict(total_views=0, total_contacts=0)
    channels, channel_w = ["WHATSAPP", "IN_APP", "CALL"], [55, 30, 15]

    for lid, _pid, agent_id, _op, _price, _min_accept, _status, published in listings:
        meta = listing_meta[lid]
        slow = agent_id in slow_agents
        interested_pool = rng.sample(client_ids, min(6, len(client_ids))) if client_ids else []

        d, end_day = max(sim_start.date(), published.date()), now.date()
        while d <= end_day:
            days_since_publish = (d - published.date()).days
            age_decay = 0.4 + 0.6 * math.exp(-days_since_publish / 45.0)
            lam = (_base_daily_rate(meta["tier"], meta["property_type"])
                   * WEEKLY[d.weekday()] * MONTHLY[d.month] * age_decay
                   * (1.6 if meta["overpriced"] else 1.0))
            n = poisson(rng, lam)
            stats["total_views"] += n

            for _ in range(n):
                ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + time_of_day(rng)
                if rng.random() < 0.25:
                    cid = (rng.choice(interested_pool) if rng.random() < 0.7 and interested_pool
                          else rng.choice(client_ids))
                else:
                    cid = None
                views_rows.append((uid(rng), lid, cid, uid(rng)[:16], ts))
                if cid is None:
                    continue

                logit = (CONTACT_B0 + CONTACT_B_PRICE * (-math.log(max(meta["price_ratio"], 0.5)))
                         + CONTACT_B_ATTR * meta["attractiveness"])
                if rng.random() >= sigmoid(logit):
                    continue
                stats["total_contacts"] += 1

                pair = (cid, lid)
                if pair in lead_by_pair:
                    duplicate_pairs.add(pair)
                    interactions.append((
                        uid(rng), lead_by_pair[pair], "INBOUND",
                        rng.choices(channels, weights=channel_w)[0], "MESSAGE",
                        "Hola, sigo interesado en este inmueble.", ts, None,
                    ))
                    continue

                lead_id = uid(rng)
                channel = rng.choices(channels, weights=channel_w)[0]
                lead_by_pair[pair] = lead_id
                transitions.append((uid(rng), lead_id, None, "INTERESTED", None, ts))
                interactions.append((uid(rng), lead_id, "INBOUND", channel, "MESSAGE",
                                     "Hola, me interesa este inmueble.", ts, None))

                final_stage = _walk_lead(
                    rng, now, lead_id, agent_id, slow, meta, ts, (30 if slow else 2),
                    transitions, interactions, appointments, feedbacks, offers,
                    deals, lost_details, used_slots,
                )
                leads.append((lead_id, cid, lid, agent_id, channel, ts, final_stage))
            d += timedelta(days=1)

    # Appointment status is derived post-hoc, RNG-free: COMPLETED if the lead
    # reached VISITED, CONFIRMED if the slot is still ahead of `now`, else
    # CANCELLED (scheduled, then the lead died before the visit happened).
    visited_leads = {tr[1] for tr in transitions if tr[3] == "VISITED"}
    for ap in appointments:
        if ap[1] in visited_leads:
            ap[5] = "COMPLETED"
        elif ap[3] > now:
            ap[5] = "CONFIRMED"
        else:
            ap[5] = "CANCELLED"

    return dict(
        leads=leads, transitions=transitions, interactions=interactions,
        appointments=[tuple(a) for a in appointments],
        feedbacks=feedbacks, offers=[tuple(o) for o in offers], deals=deals,
        lost_details=lost_details, views=views_rows,
        duplicate_pairs=duplicate_pairs, stats=stats,
    )


# ----------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------
def _csv_buf(rows) -> io.StringIO:
    """CSV, not TSV: Faker-generated text (addresses, names) can contain
    characters that break naive tab-joining. NULL is the empty string, which
    is what the COPY calls below declare via `NULL ''`."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in rows:
        w.writerow("" if v is None else (v.isoformat() if isinstance(v, (datetime, date)) else v)
                   for v in row)
    buf.seek(0)
    return buf


def _copy(cur, table: str, columns: list[str], rows) -> None:
    if not rows:
        return
    cur.copy_expert(
        f"copy {table} ({', '.join(columns)}) from stdin with (format csv, null '')",
        _csv_buf(rows),
    )


def load(conn, d: dict) -> None:
    cur = conn.cursor()

    print("wiping previous data ...")
    cur.execute("""
        truncate events.property_view,
                 core.deal, core.offer, core.visit_feedback, core.appointment,
                 core.follow_up_task, core.assignment_audit, core.lead_lost_detail,
                 core.interaction, core.lead_stage_transition, core.lead,
                 core.listing, core.property, core.client, core.owner,
                 core.agent, core.agency, pii.person cascade;
    """)

    print("loading dimensions ...")
    _copy(cur, "pii.person", ["id", "full_name", "email", "phone", "national_id"], d["persons"])
    _copy(cur, "core.agency", ["id", "name"], d["agencies"])
    _copy(cur, "core.agent", ["id", "person_id", "agency_id", "role", "active"], d["agents"])
    _copy(cur, "core.owner", ["id", "person_id"], d["owners"])
    _copy(cur, "core.client", ["id", "person_id"], d["clients"])
    _copy(cur, "core.property",
         ["id", "owner_id", "property_type", "city", "neighborhood", "address",
          "area_m2", "bedrooms", "bathrooms", "parking_spots", "year_built", "hoa_fee"],
         d["properties"])
    _copy(cur, "core.listing",
         ["id", "property_id", "agent_id", "operation_type", "asking_price",
          "min_acceptable_price", "status", "published_at"], d["listings"])

    print("disabling trg_sync_lead_stage for the bulk funnel load ...")
    cur.execute("alter table core.lead_stage_transition disable trigger trg_sync_lead_stage")

    print(f"loading funnel ({len(d['leads'])} leads, {len(d['transitions'])} transitions) ...")
    _copy(cur, "core.lead",
         ["id", "client_id", "listing_id", "agent_id", "source_channel",
          "created_at", "current_stage"],
         [(lead_id, cid, lid, agent_id, ch, created, stage)
          for (lead_id, cid, lid, agent_id, ch, created, stage) in d["leads"]])
    _copy(cur, "core.lead_stage_transition",
         ["id", "lead_id", "from_stage", "to_stage", "changed_by", "changed_at"],
         sorted(d["transitions"], key=lambda r: r[5]))
    _copy(cur, "core.interaction",
         ["id", "lead_id", "direction", "channel", "type", "body", "occurred_at", "created_by"],
         d["interactions"])
    _copy(cur, "core.appointment",
         ["id", "lead_id", "agent_id", "scheduled_at", "duration_min", "status"],
         d["appointments"])

    print("re-enabling trg_sync_lead_stage and reconciling current_stage from the log ...")
    cur.execute("alter table core.lead_stage_transition enable trigger trg_sync_lead_stage")
    cur.execute("""
        update core.lead l
           set current_stage = latest.to_stage, updated_at = now()
          from (
            select distinct on (lead_id) lead_id, to_stage
            from core.lead_stage_transition
            order by lead_id, changed_at desc
          ) latest
         where latest.lead_id = l.id
           and l.current_stage is distinct from latest.to_stage
    """)
    cur.execute("""
        select count(*) from core.lead l
        join (select distinct on (lead_id) lead_id, to_stage from core.lead_stage_transition
              order by lead_id, changed_at desc) latest on latest.lead_id = l.id
        where l.current_stage is distinct from latest.to_stage
    """)
    drift = cur.fetchone()[0]
    if drift:
        raise RuntimeError(f"current_stage drift after reconciliation: {drift} lead(s)")
    print(f"  drift check: 0 (of {len(d['leads'])} leads)")

    print("loading feedback, offers, deals, lost details, tasks, audits ...")
    cur.execute("select id, code from core.objection")
    obj = dict((code, oid) for oid, code in cur.fetchall())
    _copy(cur, "core.visit_feedback",
         ["id", "appointment_id", "submitted_by", "interest_score", "objection_id",
          "close_probability", "free_text"],
         [(fid, aid, by, score, (obj[code] if code else None), prob, txt)
          for (fid, aid, by, score, code, prob, txt) in d["feedbacks"]])
    _copy(cur, "core.offer", ["id", "lead_id", "amount", "offered_by", "status", "offered_at"],
         d["offers"])
    _copy(cur, "core.deal",
         ["lead_id", "closed_amount", "commission", "closed_at", "contract_start",
          "contract_months"], d["deals"])

    cur.execute("select id, code from core.lost_reason")
    lr = dict((code, rid) for rid, code in cur.fetchall())
    _copy(cur, "core.lead_lost_detail", ["lead_id", "lost_reason_id", "free_text"],
         [(lead_id, lr[code], txt) for (lead_id, code, txt) in d["lost_details"]])
    _copy(cur, "core.follow_up_task", ["id", "lead_id", "agent_id", "due_at", "note", "status"],
         d["tasks"])
    _copy(cur, "core.assignment_audit",
         ["id", "lead_id", "from_agent_id", "to_agent_id", "reassigned_by", "reassigned_at"],
         d["audits"])

    print(f"loading {len(d['views'])} property views ...")
    _copy(cur, "events.property_view", ["id", "listing_id", "client_id", "session_id", "viewed_at"],
         d["views"])

    conn.commit()


def _print_summary(d: dict, seed: int, scale: str, months: int, now: datetime) -> None:
    print("\n=========== GROUND TRUTH (save this) ===========")
    print(f"seed={seed} scale={scale} months={months} now={now.isoformat()}")
    print(f"totals: {len(d['leads'])} leads, {len(d['transitions'])} transitions, "
          f"{len(d['views'])} views, {len(d['deals'])} deals, "
          f"{len(d['lost_details'])} lost")
    stage_counts: dict[str, int] = {}
    for row in d["leads"]:
        stage_counts[row[6]] = stage_counts.get(row[6], 0) + 1
    print(f"lead final-stage distribution: {stage_counts}")
    print(f"raw views simulated: {d['stats']['total_views']}, "
          f"contacts: {d['stats']['total_contacts']}")
    print(f"slow agents ({len(d['slow_agents'])}): should show ~15x median first-response gap")
    for a in sorted(d["slow_agents"]):
        print(f"  {a}")
    print(f"overpriced listings ({len(d['overpriced'])}): higher views, lower win rate")
    print(f"best-converting segment listings ({len(d['best_segment'])}): "
          f"2-3 bed APARTMENT, tier 4, priced at/below zone median")
    print(f"duplicate submission pairs ({len(d['duplicate_pairs'])}): "
          f"repeat contacts on an existing (client, listing) thread")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", choices=SCALES, default="medium")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--months", type=int, default=12,
                    help="Simulation window length, in months (AC1: 12-18).")
    ap.add_argument("--now", metavar="ISO8601",
                    help="Pin the simulation's 'now'. Default: wall clock.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Generate and print the summary; do not touch the database.")
    args = ap.parse_args()

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(timezone.utc)
          if args.now else datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)

    print(f"generating: scale={args.scale} seed={args.seed} months={args.months} now={now.isoformat()}")
    d = generate(SCALES[args.scale], args.seed, args.months, now)
    _print_summary(d, args.seed, args.scale, args.months, now)

    if args.dry_run:
        return

    import psycopg2
    url = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL_MIGRATE first (Supabase session-pooler URI, port 5432).")

    conn = psycopg2.connect(url)
    try:
        load(conn, d)
    finally:
        conn.close()
    print("\nload complete.")


if __name__ == "__main__":
    main()

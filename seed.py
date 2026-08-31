#!/usr/bin/env python3
"""
Synthetic data generator for the Real Estate CRM MVP (HT-02).

Deterministic (fixed seed), injects known causal patterns so the analytics
dashboards can be validated against ground truth:
  - ~20% of agents are "slow responders"  -> worse lead->visit conversion
  - ~15% of listings are overpriced       -> high views, low conversion
  - ~12% of leads are abandoned           -> no outbound interaction, LOST/NO_RESPONSE

WARNING: load() TRUNCATEs every table before inserting.

Usage:
  pip install -r requirements.txt
  # SESSION pooler, port 5432 -- not the 6543 transaction pooler the API uses.
  export DATABASE_URL_MIGRATE='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
  python seed.py --scale small --seed 42 --now 2026-08-31T00:00:00Z

Two phases: generate() builds tuples in memory with no database at all, load()
pushes them. That is what makes the generator testable without a connection.
"""

import argparse
import io
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import execute_values
from faker import Faker

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
SCALES = {
    "small":  dict(agencies=3, agents_per_agency=6, properties=250, clients=900,  leads=1800),
    "medium": dict(agencies=5, agents_per_agency=8, properties=600, clients=2500, leads=5000),
}

SIM_MONTHS = 12
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
SIM_START = NOW - timedelta(days=30 * SIM_MONTHS)

# neighborhood -> price tier (1 cheap .. 5 premium). Location-agnostic on purpose.
NEIGHBORHOODS = [
    ("Riverside", 5), ("Old Town", 5), ("Garden Hills", 4), ("Lakeview", 4),
    ("Maplewood", 3), ("Brookfield", 3), ("Westgate", 3), ("Fairview", 2),
    ("Northfield", 2), ("Millbrook", 2), ("Easton", 1), ("Southport", 1),
]
CITY = "Central City"
BASE_PRICE_M2 = {1: 900, 2: 1300, 3: 1800, 4: 2600, 5: 3600}   # per m2, abstract currency
TYPE_FACTOR = {"APARTMENT": 1.0, "HOUSE": 1.05, "STUDIO": 0.9, "COUNTRY_HOUSE": 1.15}

SLOW_AGENT_SHARE = 0.20
OVERPRICED_SHARE = 0.15
ABANDONED_SHARE = 0.12
DUPLICATE_CLIENT_SHARE = 0.03
MISSING_EMAIL_SHARE = 0.08

fake = Faker("en_US")


def dt_jitter(base, max_hours):
    return base + timedelta(hours=random.uniform(0, max_hours))


def lognormal_hours(median_h, sigma=0.8):
    import math
    return random.lognormvariate(math.log(median_h), sigma)


def phone_variant(phone):
    digits = "".join(c for c in phone if c.isdigit())
    style = random.choice(["spaced", "dashed", "plus"])
    if style == "spaced":
        return " ".join([digits[:3], digits[3:6], digits[6:]])
    if style == "dashed":
        return "-".join([digits[:3], digits[3:6], digits[6:]])
    return "+1" + digits[-10:]


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------
def generate(cfg):
    persons, agencies, agents, owners, clients = [], [], [], [], []
    properties, listings = [], []
    leads, transitions, interactions, appointments = [], [], [], []
    feedbacks, offers, deals, lost_details, tasks, audits = [], [], [], [], [], []
    views_rows = []

    slow_agents, overpriced_listings = set(), set()
    used_slots = set()  # (agent_id, iso_slot) -> appointment overlap avoidance

    def new_person(email_ok=True, national_id=False):
        pid = str(uuid.uuid4())
        persons.append((
            pid, fake.name(),
            fake.email() if (email_ok and random.random() > MISSING_EMAIL_SHARE) else None,
            fake.numerify("##########"),
            fake.numerify("#########") if national_id else None,
        ))
        return pid

    # --- org ---
    for _ in range(cfg["agencies"]):
        aid = str(uuid.uuid4())
        agencies.append((aid, fake.company() + " Realty"))
        for j in range(cfg["agents_per_agency"]):
            agid = str(uuid.uuid4())
            agents.append((agid, new_person(), aid,
                           "TEAM_ADMIN" if j == 0 else "AGENT", True))
            if random.random() < SLOW_AGENT_SHARE:
                slow_agents.add(agid)

    # --- inventory ---
    for _ in range(cfg["properties"]):
        oid = str(uuid.uuid4())
        owners.append((oid, new_person(national_id=True)))
        ptype = random.choices(list(TYPE_FACTOR), weights=[55, 25, 12, 8])[0]
        nb, tier = random.choice(NEIGHBORHOODS)
        area = round(random.uniform(38, 70) if ptype == "STUDIO"
                     else random.uniform(55, 320), 1)
        prid = str(uuid.uuid4())
        properties.append((
            prid, oid, ptype, CITY, nb, fake.street_address(), area,
            0 if ptype == "STUDIO" else random.randint(1, 5),
            random.randint(1, 4), random.randint(0, 3),
            random.randint(1975, 2024),
            round(random.uniform(60, 400), 2) if ptype in ("APARTMENT", "STUDIO") else None,
        ))
        fair = BASE_PRICE_M2[tier] * area * TYPE_FACTOR[ptype] * random.uniform(0.9, 1.1)
        op = random.choices(["SALE", "RENT"], weights=[60, 40])[0]
        price = fair if op == "SALE" else fair * 0.005          # monthly rent
        lid = str(uuid.uuid4())
        over = random.random() < OVERPRICED_SHARE
        if over:
            overpriced_listings.add(lid)
            price *= 1.25
        published = dt_jitter(SIM_START, 24 * 30 * 6)           # first 6 months
        listings.append((
            lid, prid, random.choice(agents)[0], op,
            round(price, 2), round(price * random.uniform(0.85, 0.95), 2),
            "ACTIVE", published, tier, over,                    # tier/over stripped before insert
        ))

    # --- clients (with deliberate duplicates) ---
    for _ in range(cfg["clients"]):
        clients.append((str(uuid.uuid4()), new_person()))
    for base_cid, base_pid in random.sample(clients, int(len(clients) * DUPLICATE_CLIENT_SHARE)):
        p = next(p for p in persons if p[0] == base_pid)
        dup_pid = str(uuid.uuid4())
        persons.append((dup_pid, p[1], None, phone_variant(p[3]), None))
        clients.append((str(uuid.uuid4()), dup_pid))

    # --- behavioral views (overpriced -> MORE views: the HU-16 pattern) ---
    for (lid, *_rest, published, tier, over) in [(l[0], *l[1:8], l[7], l[8], l[9]) for l in listings]:
        active_days = max(1, (NOW - published).days)
        daily = (0.4 + 0.25 * tier) * (1.6 if over else 1.0)
        n = min(int(random.gauss(daily * active_days, daily * active_days * 0.2)), 2500)
        for _ in range(max(n, 5)):
            views_rows.append((
                str(uuid.uuid4()), lid,
                random.choice(clients)[0] if random.random() < 0.25 else None,
                uuid.uuid4().hex[:16],
                dt_jitter(published, 24 * active_days),
            ))

    # --- funnel simulation ---
    stage_seq = ["INTERESTED", "VISIT_SCHEDULED", "VISITED", "NEGOTIATING", "WON"]
    lost_reason_codes = ["PRICE", "LOCATION", "BOUGHT_ELSEWHERE", "FINANCING", "OTHER"]
    pairs_seen = set()

    for _ in range(cfg["leads"]):
        li = random.choice(listings)
        lid, agent_id, published, over = li[0], li[2], li[7], li[9]
        cid = random.choice(clients)[0]
        if (cid, lid) in pairs_seen:                            # honor UNIQUE guard
            continue
        pairs_seen.add((cid, lid))

        created = dt_jitter(published, 24 * max(1, (NOW - published).days - 30))
        lead_id = str(uuid.uuid4())
        channel = random.choices(["WHATSAPP", "IN_APP", "CALL"], weights=[55, 30, 15])[0]
        leads.append((lead_id, cid, lid, agent_id, channel, created))
        transitions.append((str(uuid.uuid4()), lead_id, None, "INTERESTED", None, created))
        interactions.append((str(uuid.uuid4()), lead_id, "INBOUND", channel, "MESSAGE",
                             "Hi, I'm interested in this listing.", created, None))

        abandoned = random.random() < ABANDONED_SHARE
        if not abandoned:
            resp_h = lognormal_hours(30 if agent_id in slow_agents else 2)
            resp_at = created + timedelta(hours=resp_h)
            interactions.append((str(uuid.uuid4()), lead_id, "OUTBOUND", channel, "MESSAGE",
                                 "Thanks for reaching out! When would you like to visit?",
                                 resp_at, agent_id))
        else:
            lost_at = created + timedelta(days=random.uniform(3, 10))
            transitions.append((str(uuid.uuid4()), lead_id, "INTERESTED", "LOST", None, lost_at))
            lost_details.append((lead_id, "NO_RESPONSE", None))
            continue

        # stage walk with conditioned transition probabilities
        p_visit = 0.45 * (0.6 if agent_id in slow_agents else 1.0) * (0.55 if over else 1.0)
        probs = {"INTERESTED": p_visit, "VISIT_SCHEDULED": 0.80,
                 "VISITED": 0.50, "NEGOTIATING": 0.50 * (0.6 if over else 1.0)}
        lead_appointment_id = None       # bound when VISIT_SCHEDULED fires
        t, stage_idx = resp_at, 0
        while stage_idx < len(stage_seq) - 1:
            cur = stage_seq[stage_idx]
            t = t + timedelta(hours=lognormal_hours({"INTERESTED": 48, "VISIT_SCHEDULED": 72,
                                                     "VISITED": 96, "NEGOTIATING": 120}[cur]))
            if t >= NOW:
                break                                            # still in-flight today
            if random.random() < probs[cur]:
                nxt = stage_seq[stage_idx + 1]
                transitions.append((str(uuid.uuid4()), lead_id, cur, nxt, agent_id, t))
                if nxt == "VISIT_SCHEDULED":
                    slot = t + timedelta(days=random.randint(1, 7))
                    slot = slot.replace(minute=0, second=0, microsecond=0,
                                        hour=random.randint(8, 18))
                    while (agent_id, slot.isoformat()) in used_slots:
                        slot += timedelta(hours=1)
                    used_slots.add((agent_id, slot.isoformat()))
                    # status is a placeholder: the post-pass below derives the real one
                    # from whether this lead actually reached VISITED. A list, not a
                    # tuple, so that pass can mutate it in place.
                    lead_appointment_id = str(uuid.uuid4())
                    appointments.append([lead_appointment_id, lead_id, agent_id, slot, 60, None])
                if nxt == "VISITED":
                    ap_id = lead_appointment_id
                    feedbacks.append((str(uuid.uuid4()), ap_id, "AGENT",
                                      random.randint(3, 5), None,
                                      round(random.uniform(0.3, 0.9), 2), None))
                    if random.random() < 0.6:
                        feedbacks.append((str(uuid.uuid4()), ap_id, "CLIENT",
                                          random.randint(2, 5),
                                          "PRICE" if over and random.random() < 0.5 else None,
                                          None, None))
                if nxt == "NEGOTIATING":
                    asking, floor = li[4], li[5]
                    for k in range(random.randint(1, 3)):
                        offers.append((str(uuid.uuid4()), lead_id,
                                       round(random.uniform(floor, asking), 2),
                                       "CLIENT" if k % 2 == 0 else "AGENT",
                                       "COUNTERED", t + timedelta(days=k)))
                if nxt == "WON":
                    amount = round(random.uniform(li[5], li[4] * 0.98), 2)
                    commission = round(amount * 0.03, 2) if li[3] == "SALE" else round(amount, 2)
                    deals.append((lead_id, amount, commission, t,
                                  t.date() if li[3] == "RENT" else None,
                                  12 if li[3] == "RENT" else None))
                    if offers and offers[-1][1] == lead_id:
                        offers[-1] = offers[-1][:4] + ("ACCEPTED",) + offers[-1][5:]
                stage_idx += 1
            else:
                transitions.append((str(uuid.uuid4()), lead_id, cur, "LOST", agent_id, t))
                lost_details.append((lead_id,
                                     "PRICE" if over and random.random() < 0.6
                                     else random.choice(lost_reason_codes), None))
                break

    # --- derive appointment status ------------------------------------------
    # Post-hoc and RNG-free, so the seed-42 counts above are unaffected. Previously
    # every appointment was hard-coded COMPLETED, including ones whose lead was
    # scheduled and then went LOST before ever visiting.
    visited_leads = {tr[1] for tr in transitions if tr[3] == "VISITED"}
    for ap in appointments:
        if ap[1] in visited_leads:
            ap[5] = "COMPLETED"
        elif ap[3] > NOW:
            ap[5] = "CONFIRMED"     # still in the future: the visit hasn't happened yet
        else:
            ap[5] = "CANCELLED"     # scheduled, then the lead died before the visit

    # --- light extras: follow-up tasks & a few reassignments ---
    for (lead_id, _c, _l, agent_id, _ch, created) in random.sample(leads, min(200, len(leads))):
        tasks.append((str(uuid.uuid4()), lead_id, agent_id,
                      created + timedelta(days=random.randint(1, 14)),
                      "Follow up with client", random.choice(["PENDING", "DONE"])))

    # Reassignment must (a) stay inside the agency -- a lead handed to another
    # company is not a reassignment -- and (b) actually move the lead. The old
    # version wrote the audit row and left lead.agent_id pointing at the old agent,
    # so the audit trail disagreed with the lead itself.
    agents_by_agency, agency_of = {}, {}
    for a in agents:
        agents_by_agency.setdefault(a[2], []).append(a[0])
        agency_of[a[0]] = a[2]
    for idx in random.sample(range(len(leads)), max(1, len(leads) // 30)):
        lead_id, cid, lid, agent_id, ch, created = leads[idx]
        pool = [x for x in agents_by_agency[agency_of[agent_id]] if x != agent_id]
        if not pool:
            continue
        other = random.choice(pool)
        audits.append((str(uuid.uuid4()), lead_id, agent_id, other, other, NOW))
        leads[idx] = (lead_id, cid, lid, other, ch, created)

    return dict(persons=persons, agencies=agencies, agents=agents, owners=owners,
                clients=clients, properties=properties, listings=listings, leads=leads,
                transitions=transitions, interactions=interactions,
                appointments=appointments, feedbacks=feedbacks, offers=offers,
                deals=deals, lost_details=lost_details, tasks=tasks, audits=audits,
                views=views_rows, slow_agents=slow_agents, overpriced=overpriced_listings)


# ----------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------
def load(conn, d):
    cur = conn.cursor()
    ev = lambda sql, rows: execute_values(cur, sql, rows, page_size=1000)

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
    ev("insert into pii.person (id, full_name, email, phone, national_id) values %s", d["persons"])
    ev("insert into core.agency (id, name) values %s", d["agencies"])
    ev("insert into core.agent (id, person_id, agency_id, role, active) values %s", d["agents"])
    ev("insert into core.owner (id, person_id) values %s", d["owners"])
    ev("insert into core.client (id, person_id) values %s", d["clients"])
    ev("""insert into core.property (id, owner_id, property_type, city, neighborhood, address,
          area_m2, bedrooms, bathrooms, parking_spots, year_built, hoa_fee) values %s""",
       d["properties"])
    ev("""insert into core.listing (id, property_id, agent_id, operation_type, asking_price,
          min_acceptable_price, status, published_at) values %s""",
       [l[:8] for l in d["listings"]])                       # strip helper cols

    print("loading funnel ...")
    ev("""insert into core.lead (id, client_id, listing_id, agent_id, source_channel, created_at)
          values %s""", d["leads"])
    ev("""insert into core.lead_stage_transition (id, lead_id, from_stage, to_stage, changed_by,
          changed_at) values %s""", sorted(d["transitions"], key=lambda r: r[5]))
    ev("""insert into core.interaction (id, lead_id, direction, channel, type, body, occurred_at,
          created_by) values %s""", d["interactions"])
    ev("""insert into core.appointment (id, lead_id, agent_id, scheduled_at, duration_min, status)
          values %s""", d["appointments"])
    ev("""insert into core.visit_feedback (id, appointment_id, submitted_by, interest_score,
          objection_id, close_probability, free_text)
          values %s""",
       [(f[0], f[1], f[2], f[3],
         None, f[5], f[6]) for f in d["feedbacks"]])   # objection_id set just below
    cur.execute("select id, code from core.objection")
    obj = dict((code, oid) for oid, code in cur.fetchall())
    price_rows = [(f[0], obj["PRICE"]) for f in d["feedbacks"] if f[4] == "PRICE"]
    if price_rows:
        # executemany here was one network round trip per row against Supabase.
        ev("""update core.visit_feedback vf set objection_id = v.oid::uuid
              from (values %s) as v(fid, oid) where vf.id = v.fid::uuid""", price_rows)
    ev("insert into core.offer (id, lead_id, amount, offered_by, status, offered_at) values %s",
       d["offers"])
    ev("""insert into core.deal (lead_id, closed_amount, commission, closed_at, contract_start,
          contract_months) values %s""", d["deals"])
    cur.execute("select id, code from core.lost_reason")
    lr = dict((code, rid) for rid, code in cur.fetchall())
    ev("insert into core.lead_lost_detail (lead_id, lost_reason_id, free_text) values %s",
       [(x[0], lr[x[1]], x[2]) for x in d["lost_details"]])
    ev("insert into core.follow_up_task (id, lead_id, agent_id, due_at, note, status) values %s",
       d["tasks"])
    ev("""insert into core.assignment_audit (id, lead_id, from_agent_id, to_agent_id,
          reassigned_by, reassigned_at) values %s""", d["audits"])

    print(f"loading {len(d['views'])} property views (COPY) ...")
    buf = io.StringIO()
    for r in d["views"]:
        buf.write("\t".join([r[0], r[1], r[2] or "\\N", r[3], r[4].isoformat()]) + "\n")
    buf.seek(0)
    cur.copy_expert(
        "copy events.property_view (id, listing_id, client_id, session_id, viewed_at) from stdin",
        buf)

    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", choices=SCALES, default="small")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--now", metavar="ISO8601",
                    help="Pin the simulation's 'now' (e.g. 2026-08-31T00:00:00Z). The "
                         "generator is translation-invariant, so this does NOT change any "
                         "row counts -- it shifts every timestamp as a block. Pin it when "
                         "you need absolute timestamps to be reproducible across runs on "
                         "different days, e.g. comparing two seeded databases or asserting "
                         "on time-relative behaviour. Default is wall-clock.")
    args = ap.parse_args()

    if args.now:
        global NOW, SIM_START
        NOW = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
               .astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0))
        SIM_START = NOW - timedelta(days=30 * SIM_MONTHS)

    random.seed(args.seed)
    Faker.seed(args.seed)

    # Seeding uses the SESSION pooler (5432). The transaction pooler (6543) is for the
    # API at runtime and cannot run COPY the way this loader does.
    url = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL_MIGRATE first (Supabase session-pooler URI, port 5432).")

    d = generate(SCALES[args.scale])
    conn = psycopg2.connect(url)
    try:
        load(conn, d)
    finally:
        conn.close()

    print("\n=========== GROUND TRUTH (save this) ===========")
    print(f"seed={args.seed} scale={args.scale} now={NOW.isoformat()}"
          f"{'' if args.now else '  (WALL CLOCK - pass --now to reproduce this run)'}")
    print(f"slow agents ({len(d['slow_agents'])}): should show ~30h median response "
          f"and ~40% lower lead->visit conversion")
    for a in sorted(d["slow_agents"]):
        print(f"  {a}")
    print(f"overpriced listings ({len(d['overpriced'])}): should show high views, "
          f"low visit & win rates, losses dominated by PRICE")
    print(f"abandoned leads: ~{int(ABANDONED_SHARE*100)}% of leads, LOST with NO_RESPONSE "
          f"and zero OUTBOUND interactions")
    print(f"totals: {len(d['leads'])} leads, {len(d['transitions'])} transitions, "
          f"{len(d['views'])} views, {len(d['deals'])} deals")


if __name__ == "__main__":
    main()

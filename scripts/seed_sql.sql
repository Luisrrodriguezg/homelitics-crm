-- ============================================================================
-- seed_sql.sql — server-side dataset generator for homelitics.
--
-- WHY THIS EXISTS ALONGSIDE seed.py
--   seed.py needs a Postgres connection string (DATABASE_URL_MIGRATE) because it
--   pushes rows over the wire with COPY. This file needs none: every row is
--   generated inside Postgres, so it runs from the Supabase SQL Editor or the
--   MCP connector with no credentials in hand.
--
-- WHAT IT PRODUCES
--   ~499,000 rows: 6 agencies, 48 agents, 1,200 listings, 6,181 clients,
--   13,000 leads, 375,281 property views. See docs/ground_truth.md.
--
-- DESTRUCTIVE. Stage 0 truncates every table. Do not run it against anything
-- you care about.
--
-- NOT ROW-FOR-ROW REPRODUCIBLE: it uses random(). The *cohorts* are
-- deterministic (ordinal rules on immutable ids), so the injected patterns
-- reproduce; exact counts will not. If you need reproducibility, use
-- `seed.py --seed 42` instead — but note that seed.py TRUNCATEs first too.
--
-- Run the stages IN ORDER. Each is independently re-runnable only after a
-- fresh Stage 0.
-- ============================================================================


-- === STAGE 0: wipe, and take the sync trigger out of the hot path ==========
-- The trigger runs a max() subquery per inserted transition. For 32k rows that
-- is pure overhead, and the load inserts transitions in bulk rather than in
-- changed_at order. Stage 8 reconciles current_stage from the log and turns the
-- trigger back on.
truncate events.property_view,
         core.deal, core.offer, core.visit_feedback, core.appointment,
         core.follow_up_task, core.assignment_audit, core.lead_lost_detail,
         core.interaction, core.lead_stage_transition, core.lead,
         core.listing, core.property, core.client, core.owner,
         core.agent, core.agency, pii.person cascade;

alter table core.lead_stage_transition disable trigger trg_sync_lead_stage;


-- === STAGE 1: agencies + agents ============================================
insert into core.agency (name)
select (array['Vanguard','Northgate','Solaris','Brickfield','Meridian','Kestrel'])[g] || ' Realty'
from generate_series(1,6) g;

with fn as (select array['Ana','Luis','Marta','Diego','Sofia','Carlos','Elena','Javier','Lucia','Miguel',
                         'Clara','Andres','Paula','Tomas','Irene','Rafael','Nuria','Pablo','Alba','Sergio'] a),
     ln as (select array['Garcia','Rodriguez','Martinez','Lopez','Sanchez','Perez','Gomez','Fernandez',
                         'Ruiz','Diaz','Moreno','Alvarez','Romero','Navarro','Torres','Ramos','Vega',
                         'Castro','Ortega','Silva'] a),
src as (
  select gen_random_uuid() as person_id, gen_random_uuid() as agent_id,
         ag.id as agency_id, i as idx,
         (select a from fn)[1 + ((ag.ord*8 + i) * 7) % 20] || ' ' ||
         (select a from ln)[1 + ((ag.ord*8 + i) * 13) % 20] as full_name,
         ag.ord*8 + i as seq
  from (select id, row_number() over (order by name) - 1 as ord from core.agency) ag
  cross join generate_series(1,8) i
),
ins as (
  insert into pii.person (id, full_name, email, phone)
  select person_id, full_name,
         case when seq % 12 = 0 then null      -- ~8% missing emails, on purpose
              else lower(replace(full_name,' ','.')) || seq || '@realty.example' end,
         '+1' || lpad((6000000000 + seq*7919)::text, 10, '0')
  from src
)
insert into core.agent (id, person_id, agency_id, role, active)
select agent_id, person_id, agency_id,
       case when idx = 1 then 'TEAM_ADMIN' else 'AGENT' end, true
from src;


-- === STAGE 2a: owners + properties =========================================
with src as (
  select i, gen_random_uuid() as person_id, gen_random_uuid() as owner_id,
         gen_random_uuid() as property_id,
         (array['APARTMENT','HOUSE','STUDIO','COUNTRY_HOUSE'])
           [case when i%100 < 55 then 1 when i%100 < 80 then 2 when i%100 < 92 then 3 else 4 end] as ptype,
         (array['Riverside','Old Town','Garden Hills','Lakeview','Maplewood','Brookfield',
                'Westgate','Fairview','Northfield','Millbrook','Easton','Southport'])[1 + i % 12] as neighborhood
  from generate_series(1,1200) i
),
dim as (
  select src.*,
         round((case when ptype='STUDIO' then 38 + random()*32
                     else 55 + random()*265 end)::numeric, 1) as area,
         case when ptype='STUDIO' then 0 else 1 + floor(random()*5)::int end as beds
  from src
),
ins_person as (
  insert into pii.person (id, full_name, email, phone, national_id)
  select person_id,
         'Owner ' || i || ' ' || (array['Nunez','Iglesias','Prieto','Cano','Reyes','Bravo','Marin','Cortes'])[1+i%8],
         case when i % 12 = 0 then null else 'owner' || i || '@mail.example' end,
         '+1' || lpad((7000000000 + i*104729)::text, 10, '0'),
         lpad((100000000 + i*37)::text, 9, '0')
  from dim
),
ins_owner as (insert into core.owner (id, person_id) select owner_id, person_id from dim)
insert into core.property (id, owner_id, property_type, city, neighborhood, address,
                           area_m2, bedrooms, bathrooms, parking_spots, year_built, hoa_fee)
select property_id, owner_id, ptype, 'Central City', neighborhood,
       (100 + i) || ' ' || (array['Oak','Maple','Cedar','Birch','Elm','Pine','Willow','Ash'])[1+i%8] || ' St',
       area, beds, 1 + floor(random()*4)::int, floor(random()*4)::int,
       1975 + floor(random()*50)::int,
       case when ptype in ('APARTMENT','STUDIO') then round((60 + random()*340)::numeric, 2) end
from dim;


-- === STAGE 2b: listings, then inflate the overpriced cohort ================
with rate(tier, per_m2) as (values (1,900::numeric),(2,1300),(3,1800),(4,2600),(5,3600)),
nb(name, tier) as (values
  ('Riverside',5),('Old Town',5),('Garden Hills',4),('Lakeview',4),
  ('Maplewood',3),('Brookfield',3),('Westgate',3),('Fairview',2),
  ('Northfield',2),('Millbrook',2),('Easton',1),('Southport',1)),
tf(ptype, f) as (values ('APARTMENT',1.00::numeric),('HOUSE',1.05),('STUDIO',0.90),('COUNTRY_HOUSE',1.15)),
agents as (select id, row_number() over (order by id) as ord from core.agent),
p as (
  select pr.id, pr.area_m2, pr.property_type, pr.neighborhood,
         (row_number() over (order by pr.id))::int as ord
  from core.property pr
),
priced as (
  select p.id as property_id, p.ord,
         round(rate.per_m2 * p.area_m2 * tf.f * (0.92 + random()*0.16)::numeric, 2) as price
  from p
  join nb on nb.name = p.neighborhood
  join rate on rate.tier = nb.tier
  join tf on tf.ptype = p.property_type
)
insert into core.listing (property_id, agent_id, operation_type, asking_price,
                          min_acceptable_price, status, published_at)
select priced.property_id,
       (select a.id from agents a where a.ord = 1 + (priced.ord % 48)),
       case when priced.ord % 100 < 60 then 'SALE' else 'RENT' end,
       priced.price,
       round(priced.price * (0.86 + random()*0.10)::numeric, 2),
       case when priced.ord % 25 = 0 then 'PAUSED'
            when priced.ord % 40 = 0 then 'CLOSED' else 'ACTIVE' end,
       now() - make_interval(days => 30 + (priced.ord * 7) % 330)
from priced;

-- OVERPRICED COHORT: ordinal % 7 -> ~14% of listings, +25% over fair value.
update core.listing l
set asking_price = round(l.asking_price * 1.25, 2)
from (select id, row_number() over (order by id) as ord from core.listing) o
where o.id = l.id and o.ord % 7 = 0;


-- === STAGE 3: clients, plus a ~3% duplicate cohort =========================
with fn(a) as (values (array['Ana','Luis','Marta','Diego','Sofia','Carlos','Elena','Javier','Lucia','Miguel',
                             'Clara','Andres','Paula','Tomas','Irene','Rafael','Nuria','Pablo','Alba','Sergio',
                             'Rocio','Hugo','Vera','Mateo','Olga'])),
     ln(a) as (values (array['Garcia','Rodriguez','Martinez','Lopez','Sanchez','Perez','Gomez','Fernandez',
                             'Ruiz','Diaz','Moreno','Alvarez','Romero','Navarro','Torres','Ramos','Vega',
                             'Castro','Ortega','Silva','Blanco','Molina','Serrano','Rubio','Pena'])),
src as (
  select i, gen_random_uuid() as person_id, gen_random_uuid() as client_id,
         (select a from fn)[1 + (i*11) % 25] || ' ' || (select a from ln)[1 + (i*17) % 25] as full_name,
         6000000000 + i*7919 as phone_num
  from generate_series(1,6000) i
),
ins_p as (
  insert into pii.person (id, full_name, email, phone)
  select person_id, full_name,
         case when i % 12 = 0 then null
              else lower(replace(full_name,' ','.')) || i || '@mail.example' end,
         '+1' || lpad(phone_num::text, 10, '0')
  from src
),
ins_c as (insert into core.client (id, person_id) select client_id, person_id from src),
-- same human, same digits, different formatting: a dedup test fixture
dup as (
  select i, gen_random_uuid() as person_id, gen_random_uuid() as client_id, full_name, phone_num
  from src where i % 33 = 0
),
ins_dp as (
  insert into pii.person (id, full_name, email, phone)
  select person_id, full_name, lower(replace(full_name,' ','.')) || i || '@mail.example',
         '(' || substr(lpad(phone_num::text,10,'0'),1,3) || ') '
             || substr(lpad(phone_num::text,10,'0'),4,3) || '-'
             || substr(lpad(phone_num::text,10,'0'),7,4)
  from dup
)
insert into core.client (id, person_id) select client_id, person_id from dup;


-- === STAGE 4a: the funnel plan =============================================
-- One row per lead holding every random decision up front, so stages 4b-6 are
-- deterministic reads instead of fresh random() draws that would disagree with
-- each other.
drop table if exists public.seed_plan;
create table public.seed_plan (
  lead_id uuid primary key, client_id uuid not null, listing_id uuid not null,
  agent_id uuid not null, created_at timestamptz not null, channel text not null,
  is_slow boolean not null, is_over boolean not null, abandoned boolean not null,
  resp_hours numeric, depth int not null, ended_lost boolean not null,
  step_h numeric[] not null,
  unique (client_id, listing_id)   -- mirrors core.lead's real UNIQUE guard
);

with cl as (select id, row_number() over (order by id) as ord from core.client),
     li as (select l.id, l.agent_id, (row_number() over (order by l.id))::int as ord from core.listing l),
     ag as (select id, (row_number() over (order by id))::int as ord from core.agent),
cand as (
  -- 4099 is coprime with the client count, so pairs spread instead of clustering
  select i, 1 + (i % 1200) as l_ord, 1 + ((i * 4099) % 6181) as c_ord
  from generate_series(1, 13000) i
),
drawn as (
  select gen_random_uuid() as lead_id, cl.id as client_id, li.id as listing_id, li.agent_id,
         (ag.ord % 5 = 0) as is_slow,   -- SLOW COHORT: 20% of agents
         (li.ord % 7 = 0) as is_over,   -- OVERPRICED COHORT: matches stage 2b
         random() as r1, random() as r2, random() as r3,
         random() as r4, random() as r5, random() as r6,
         -- >=35 days back so a full 4-step walk always lands in the past
         now() - make_interval(days => (35 + floor(random()*295))::int) as created_at
  from cand
  join li on li.ord = cand.l_ord
  join cl on cl.ord = cand.c_ord
  join ag on ag.id = li.agent_id
),
computed as (
  select d.*, (d.r6 < 0.12) as abandoned,
         0.45 * (case when d.is_slow then 0.6 else 1.0 end)
              * (case when d.is_over then 0.55 else 1.0 end) as p1,
         0.50 * (case when d.is_over then 0.6 else 1.0 end) as p4
  from drawn d
),
final as (
  select c.*,
         case when c.abandoned then 0
              when c.r1 >= c.p1 then 0 when c.r2 >= 0.80 then 1
              when c.r3 >= 0.50 then 2 when c.r4 >= c.p4 then 3
              else 4 end as depth
  from computed c
)
insert into public.seed_plan
select lead_id, client_id, listing_id, agent_id, created_at,
       (array['WHATSAPP','IN_APP','CALL'])[case when r5 < 0.55 then 1 when r5 < 0.85 then 2 else 3 end],
       is_slow, is_over, abandoned,
       -- slow agents answer around 30h, everyone else around 2h
       case when abandoned then null
            else round((case when is_slow then 30 else 2 end) * (0.4 + r1*1.6)::numeric, 2) end,
       depth,
       case when abandoned then true else depth < 4 and r5 < 0.75 end,
       array[round((24 + r2*96)::numeric,1), round((24 + r3*120)::numeric,1),
             round((48 + r4*120)::numeric,1), round((48 + r1*144)::numeric,1)]
from final
on conflict (client_id, listing_id) do nothing;


-- === STAGE 4b/4c: leads and the transition log =============================
insert into core.lead (id, client_id, listing_id, agent_id, source_channel, current_stage, created_at, updated_at)
select lead_id, client_id, listing_id, agent_id, channel, 'INTERESTED', created_at, created_at
from public.seed_plan;

insert into core.lead_stage_transition (lead_id, from_stage, to_stage, changed_by, changed_at)
select lead_id, null, 'INTERESTED', null, created_at from public.seed_plan;

insert into core.lead_stage_transition (lead_id, from_stage, to_stage, changed_by, changed_at)
select p.lead_id,
       (array['INTERESTED','VISIT_SCHEDULED','VISITED','NEGOTIATING','WON'])[k],
       (array['INTERESTED','VISIT_SCHEDULED','VISITED','NEGOTIATING','WON'])[k+1],
       p.agent_id,
       p.created_at + coalesce(p.resp_hours,0) * interval '1 hour' + cum.h * interval '1 hour'
from public.seed_plan p
cross join lateral generate_series(1, p.depth) k
cross join lateral (select sum(v) as h from unnest(p.step_h[1:k]) v) cum
where p.depth > 0;

insert into core.lead_stage_transition (lead_id, from_stage, to_stage, changed_by, changed_at)
select p.lead_id,
       (array['INTERESTED','VISIT_SCHEDULED','VISITED','NEGOTIATING','WON'])[p.depth+1],
       'LOST',
       case when p.abandoned then null else p.agent_id end,
       case when p.abandoned
            then p.created_at + (72 + random()*168)::numeric * interval '1 hour'
            else p.created_at + coalesce(p.resp_hours,0) * interval '1 hour'
                 + coalesce((select sum(v) from unnest(p.step_h[1:p.depth]) v),0) * interval '1 hour'
                 + p.step_h[least(p.depth+1,4)] * interval '1 hour'
       end
from public.seed_plan p
where p.ended_lost;


-- === STAGE 5: interactions =================================================
insert into core.interaction (lead_id, direction, channel, type, body, occurred_at, created_by)
select lead_id, 'INBOUND', channel, 'MESSAGE',
       'Hi, I am interested in this listing. Is it still available?', created_at, null
from public.seed_plan;

-- ABANDONED COHORT gets no OUTBOUND at all: that is the 'never answered' signal.
insert into core.interaction (lead_id, direction, channel, type, body, occurred_at, created_by)
select lead_id, 'OUTBOUND', channel, 'MESSAGE',
       'Thanks for reaching out! When would you like to visit?',
       created_at + resp_hours * interval '1 hour', agent_id
from public.seed_plan where not abandoned;

insert into core.interaction (lead_id, direction, channel, type, body, occurred_at, created_by)
select p.lead_id,
       case when k % 2 = 0 then 'OUTBOUND' else 'INBOUND' end,
       p.channel, case when k = 2 then 'CALL' else 'MESSAGE' end,
       (array['Could we arrange a viewing this week?','Confirmed, see you at the property.',
              'What is the lowest the owner would accept?',
              'I will check with the owner and get back to you.'])[k],
       p.created_at + coalesce(p.resp_hours,0) * interval '1 hour'
         + cum.h * interval '1 hour' + interval '3 hour',
       case when k % 2 = 0 then p.agent_id else null end
from public.seed_plan p
cross join lateral generate_series(1, p.depth) k
cross join lateral (select sum(v) as h from unnest(p.step_h[1:k]) v) cum
where p.depth > 0 and not p.abandoned;


-- === STAGE 6a: appointments + visit feedback ===============================
with base as (
  select p.*, gen_random_uuid() as appt_id,
         p.created_at + coalesce(p.resp_hours,0) * interval '1 hour'
                      + p.step_h[1] * interval '1 hour' as scheduled_marker
  from public.seed_plan p where p.depth >= 1
),
slotted as (
  select b.*,
         date_trunc('hour', b.scheduled_marker
           + make_interval(days => 1 + (abs(hashtext(b.lead_id::text)) % 7)))
           + make_interval(hours => 8 + (abs(hashtext(b.lead_id::text)) % 10)) as slot
  from base b
),
ins_appt as (
  insert into core.appointment (id, lead_id, agent_id, scheduled_at, duration_min, status, created_at, updated_at)
  select appt_id, lead_id, agent_id, slot, 60,
         case when depth >= 2   then 'COMPLETED'    -- actually visited
              when ended_lost   then 'CANCELLED'    -- died before the visit
              when slot > now() then 'CONFIRMED'
              else 'NO_SHOW' end,
         scheduled_marker, scheduled_marker
  from slotted returning id
)
insert into core.visit_feedback (appointment_id, submitted_by, interest_score, objection_id,
                                 close_probability, free_text, created_at)
select s.appt_id, 'AGENT', 3 + (abs(hashtext(s.lead_id::text)) % 3), null,
       round((0.30 + (abs(hashtext(s.lead_id::text)) % 60)::numeric / 100.0), 2), null,
       s.slot + interval '2 hour'
from slotted s where s.depth >= 2;

-- client feedback on ~60%, PRICE objections concentrated on the overpriced cohort
insert into core.visit_feedback (appointment_id, submitted_by, interest_score, objection_id,
                                 close_probability, free_text, created_at)
select a.id, 'CLIENT', 2 + (abs(hashtext(a.id::text)) % 4),
       (select o.id from core.objection o
         where o.code = case when p.is_over and (abs(hashtext(a.id::text)) % 10) < 6 then 'PRICE'
                             else (array['SIZE','LOCATION','CONDITION','HOA_FEE','OTHER'])
                                    [1 + abs(hashtext(a.id::text)) % 5] end),
       null, null, a.scheduled_at + interval '3 hour'
from core.appointment a
join public.seed_plan p on p.lead_id = a.lead_id
where a.status = 'COMPLETED' and (abs(hashtext(a.id::text)) % 10) < 6;


-- === STAGE 6b: offers, deals, lost reasons, tasks, reassignments ===========
insert into core.offer (lead_id, amount, offered_by, status, offered_at)
select p.lead_id,
       round(l.min_acceptable_price + (l.asking_price - l.min_acceptable_price)
             * ((abs(hashtext(p.lead_id::text || k::text)) % 100)::numeric / 100.0), 2),
       case when k % 2 = 1 then 'CLIENT' else 'AGENT' end,
       case when p.depth = 4 and k = 1 + abs(hashtext(p.lead_id::text)) % 2 then 'ACCEPTED'
            else 'COUNTERED' end,
       p.created_at + coalesce(p.resp_hours,0) * interval '1 hour'
         + coalesce((select sum(v) from unnest(p.step_h[1:3]) v),0) * interval '1 hour'
         + make_interval(days => k)
from public.seed_plan p
join core.listing l on l.id = p.listing_id
cross join lateral generate_series(1, 1 + abs(hashtext(p.lead_id::text)) % 3) k
where p.depth >= 3;

insert into core.deal (lead_id, closed_amount, commission, closed_at, contract_start, contract_months)
select p.lead_id, amt.v,
       case when l.operation_type = 'SALE' then round(amt.v * 0.03, 2) else round(amt.v, 2) end,
       t.changed_at,
       case when l.operation_type = 'RENT' then t.changed_at::date end,
       case when l.operation_type = 'RENT' then 12 end
from public.seed_plan p
join core.listing l on l.id = p.listing_id
join core.lead_stage_transition t on t.lead_id = p.lead_id and t.to_stage = 'WON'
cross join lateral (select round(l.min_acceptable_price
       + (l.asking_price * 0.98 - l.min_acceptable_price)
       * ((abs(hashtext(p.lead_id::text)) % 100)::numeric / 100.0), 2) as v) amt
where p.depth = 4;

insert into core.lead_lost_detail (lead_id, lost_reason_id, free_text, created_at)
select p.lead_id,
       (select r.id from core.lost_reason r where r.code =
          case when p.abandoned then 'NO_RESPONSE'
               when p.is_over and (abs(hashtext(p.lead_id::text)) % 10) < 6 then 'PRICE'
               else (array['LOCATION','BOUGHT_ELSEWHERE','FINANCING','OTHER','PRICE'])
                      [1 + abs(hashtext(p.lead_id::text)) % 5] end),
       null, t.changed_at
from public.seed_plan p
join core.lead_stage_transition t on t.lead_id = p.lead_id and t.to_stage = 'LOST'
where p.ended_lost;

insert into core.follow_up_task (lead_id, agent_id, due_at, note, status, created_at)
select p.lead_id, p.agent_id,
       p.created_at + make_interval(days => 1 + abs(hashtext(p.lead_id::text)) % 14),
       'Follow up with client',
       case when p.ended_lost or p.depth = 4 then 'DONE'
            when abs(hashtext(p.lead_id::text)) % 5 = 0 then 'SNOOZED' else 'PENDING' end,
       p.created_at
from public.seed_plan p
where abs(hashtext(p.lead_id::text)) % 7 = 0;

-- Reassignment stays INSIDE the agency, and moves the lead as well as writing
-- the audit row. Writing only the audit is the bug this mirrors a fix for.
with pick as (
  select p.lead_id, p.agent_id as from_agent, a2.id as to_agent,
         row_number() over (partition by p.lead_id order by a2.id) as rn, p.created_at
  from public.seed_plan p
  join core.agent a1 on a1.id = p.agent_id
  join core.agent a2 on a2.agency_id = a1.agency_id and a2.id <> a1.id and a2.active
  where abs(hashtext(p.lead_id::text)) % 31 = 0
)
insert into core.assignment_audit (lead_id, from_agent_id, to_agent_id, reassigned_by, reassigned_at)
select lead_id, from_agent, to_agent, to_agent, created_at + interval '5 day'
from pick where rn = 1 + abs(hashtext(lead_id::text)) % 3;

update core.lead l
set agent_id = aa.to_agent_id, updated_at = aa.reassigned_at
from core.assignment_audit aa where aa.lead_id = l.id;


-- === STAGE 7: property views ===============================================
-- The overpriced cohort deliberately draws MORE views while converting worse.
with sample_clients as (select array_agg(id) as ids from (select id from core.client limit 500) z),
li as (select l.id, l.published_at, ((row_number() over (order by l.id))::int % 7 = 0) as is_over
       from core.listing l),
sized as (
  select li.*,
         case when li.is_over then 380 + (abs(hashtext(li.id::text)) % 160)
              else            210 + (abs(hashtext(li.id::text)) % 160) end as n_views,
         extract(epoch from (now() - li.published_at)) as span_s
  from li
)
insert into events.property_view (listing_id, client_id, session_id, viewed_at)
select s.id,
       case when (k % 10) < 3
            then (select ids from sample_clients)[1 + (abs(hashtext(s.id::text || k::text)) % 500)] end,
       'sess-' || substr(md5(s.id::text || k::text), 1, 16),
       s.published_at + (s.span_s * ((abs(hashtext(s.id::text || k::text)) % 10000)::numeric / 10000.0))
                        * interval '1 second'
from sized s
cross join lateral generate_series(1, s.n_views) k;


-- === STAGE 8: upcoming visits, reconcile the cache, trigger back on ========
insert into core.appointment (lead_id, agent_id, scheduled_at, duration_min, status)
select p.lead_id, l.agent_id,
       date_trunc('hour', now() + make_interval(days => 1 + abs(hashtext(p.lead_id::text)) % 21))
         + make_interval(hours => 9 + abs(hashtext(p.lead_id::text)) % 9),
       60,
       case when abs(hashtext(p.lead_id::text)) % 2 = 0 then 'PENDING_CONFIRMATION' else 'CONFIRMED' end
from public.seed_plan p
join core.lead l on l.id = p.lead_id
where not p.ended_lost and p.depth < 4 and p.depth <= 1
  and abs(hashtext(p.lead_id::text)) % 6 = 0;

-- current_stage is a CACHE; the log is the truth. Rebuild it from the log.
update core.lead l
set current_stage = latest.to_stage,
    updated_at    = greatest(l.updated_at, latest.changed_at)
from (
  select distinct on (t.lead_id) t.lead_id, t.to_stage, t.changed_at
  from core.lead_stage_transition t
  order by t.lead_id, t.changed_at desc, t.id desc
) latest
where latest.lead_id = l.id and l.current_stage is distinct from latest.to_stage;

alter table core.lead_stage_transition enable trigger trg_sync_lead_stage;
drop table if exists public.seed_plan;


-- === VERIFY (all four must be 0) ===========================================
select (select count(*) from core.lead l
        join lateral (select t.to_stage from core.lead_stage_transition t
                      where t.lead_id=l.id order by t.changed_at desc, t.id desc limit 1) x on true
        where l.current_stage <> x.to_stage)                                    as cache_drift,
       (select count(*) from (select client_id,listing_id from core.lead
        group by 1,2 having count(*)>1) d)                                      as dup_lead_pairs,
       (select count(*) from core.lead l where l.current_stage='LOST'
        and not exists (select 1 from core.lead_lost_detail d where d.lead_id=l.id)) as lost_without_reason,
       (select count(*) from core.assignment_audit aa
        join core.agent a1 on a1.id=aa.from_agent_id
        join core.agent a2 on a2.id=aa.to_agent_id
        where a1.agency_id<>a2.agency_id)                                       as cross_agency_reassign;

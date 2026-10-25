# Demo agency — Ramírez PLC Realty

For the sales pitch we test and demo **one** agency end to end. The product is
still a multi-tenant platform; this only fixes which tenant the team logs into.

| | |
|---|---|
| Agency | **Ramírez PLC Realty** |
| `agency_id` | `3c835dc0-d944-4fa5-80e9-ab30ed2662e9` |
| API | `https://homelitics-api.onrender.com` (free tier: first request after idle takes ~1 min) |
| Supabase URL | `https://msmpbounvqtfobcfyffr.supabase.co` |

## Why this one

All six seeded agencies are near-identical in volume (~200 listings, ~1,500
leads, 8 agents each). What separates them is whether the data tells a story a
manager would pay to see. Measured read-only on the live DB, 2026-10-22:

- **One slow agent with live work.** Inés Gabriela Caicedo answers in a median
  **31.3 h** against **2.0 h** for the rest of the team. Her leads reach a visit
  **11.7 %** of the time (team: 27.5 %) and close **1.2 %** (team: 6.3 %) — 3 wins
  from 248 leads. She still holds **14 open leads**, more than any slow agent in
  the other agencies. That is the pitch: *"the CRM shows you which agent is
  costing you deals, and which of their leads you can still save."*
- **The fullest late funnel.** 65 open leads: 30 INTERESTED, 11 VISIT_SCHEDULED,
  16 VISITED, 8 NEGOTIATING. The board has something in every column.
- **The biggest catalogue**: 207 listings (140 sale, 67 rent), 49 of them priced
  >15 % above their neighbourhood — the "lots of views, few wins" signal.
- 1,611 leads, 89 won (COP 65.0 bn closed). Top lost reason: `NO_RESPONSE` (322).

## Logins

Every login below already exists in Supabase Auth and is bound to its agent.
**All share one password.** It is not in this repo — ask Luis for it, and don't
paste it in the group chat or an issue.

| Role | Name | Email | Use it to demo |
|---|---|---|---|
| TEAM_ADMIN | Carlos Juan Restrepo Pulido | `carlos.juan.restrepo.pulido@ramirez-plc.homelitics.test` | the manager view: analytics, reassignment, invites |
| AGENT (slow) | Inés Gabriela Caicedo | `ines.gabriela.caicedo@ramirez-plc.homelitics.test` | the problem: 14 open leads, 31 h median response |
| AGENT | Liliana Rengifo Ortiz | `liliana.rengifo.ortiz@ramirez-plc.homelitics.test` | a good agent's board (10 open, 16 won) |
| AGENT | Alejandra Ofelia Jaramillo Escobar | `alejandra.ofelia.jaramillo.escobar@ramirez-plc.homelitics.test` | biggest book (258 leads, 10 open) |
| AGENT | Fidel Alberto Becerra | `fidel.alberto.becerra@ramirez-plc.homelitics.test` | fastest responder (1.7 h) — reassignment target |
| AGENT | Gladis Miriam Pérez | `gladis.miriam.perez@ramirez-plc.homelitics.test` | spare |
| AGENT | Carlos Jesús Tamayo Murillo | `carlos.jesus.tamayo.murillo@ramirez-plc.homelitics.test` | spare |
| AGENT | César Alirio Sánchez Salamanca | `cesar.alirio.sanchez.salamanca@ramirez-plc.homelitics.test` | spare |

The `.homelitics.test` domain is fake; no mail is ever sent to it.

Get a token (the anon key is public; `docs/API_GUIDE.md` §2a):

```bash
TOKEN=$(curl -s -X POST "https://msmpbounvqtfobcfyffr.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d '{"email":"carlos.juan.restrepo.pulido@ramirez-plc.homelitics.test","password":"<ask Luis>"}' \
  | jq -r .access_token)
curl -s https://homelitics-api.onrender.com/me -H "Authorization: Bearer $TOKEN"
```

**The Telegram bot / AI model** logs in as `bot@ramirez-plc.homelitics.test`
(service account `ai-ramirez-plc`; password from Luis) and sends
`X-Agency-Id: 3c835dc0-d944-4fa5-80e9-ab30ed2662e9` on every request, exactly
as it did with `ai-agent` (`docs/API_GUIDE.md` §2d). This account exists only in
Ramírez, so any other agency id is a 403 — the model cannot reach another
agency's data even if it sends the wrong header. It may create clients, leads,
timeline notes and visit requests and leave client feedback; it cannot close a
lead (WON/LOST) or confirm a visit. Created by `scripts/provision_agency_bots.sql`.
The shared `ai-agent` is being retired; don't use it.

## Before you demo

- **The history stops at 2026-08-31** (the seed's clock). Since then only test
  traffic has been written, so every open lead looks weeks old, `/leads/at-risk`
  is long, and there are **no future visits** on anyone's calendar. Book a couple
  of visits through the API the day before so the calendar isn't empty.
- **Don't re-seed.** Both seeders TRUNCATE, which unbinds every login above.
- Whatever you create while testing (clients, leads, visits) lands in the real
  Ramírez data and shows up in its analytics. Keep it small, or note what you
  made so it can be cleaned up with `scripts/purge_test_rows.sql`.
- Resume the Supabase project in the dashboard if it has been idle for 7 days.

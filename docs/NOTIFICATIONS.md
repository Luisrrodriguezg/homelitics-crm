# Notifications — frontend guide

The CRM has an in-app inbox, like a mail app. When **the Telegram bot (AI agent)
or an admin** hands an agent work, that agent gets a message:

- **a lead is assigned to them**: the bot opened a lead on their listing, or an
  admin reassigned a lead to them;
- **a visit is scheduled for them**: the bot or an admin booked a visit on one
  of their leads.

Nobody gets a message about something they did themselves. A regular agent's own
actions never create messages. There is no email and no push: the frontend asks
the API.

Two screens use this:

| Screen | Who | Endpoint |
|---|---|---|
| **Bell + inbox** | every agent (and admins, for their own messages) | `GET /me/notifications` |
| **Monitor** | `TEAM_ADMIN` only | `GET /notifications` |

---

## 1. Basics

- **Base URL:** `https://homelitics-api.onrender.com` (local: `http://localhost:8000`)
- **Auth:** `Authorization: Bearer <Supabase access token>`, the same as every
  other call. Locally with `DEV_AUTH_BYPASS`, send `X-Dev-Agent-Id: <agent uuid>`.
- **Times** are ISO-8601 UTC (`2026-10-30T15:00:00Z`). Show them in
  `America/Bogota`.
- **Free hosting sleeps:** the first request after ~15 idle minutes can take
  ~1 minute. Keep the bell's loading state quiet.

---

## 2. The message object

Both endpoints return the same page shape:

```json
{
  "items": [
    {
      "id": 1287,
      "type": "VISIT_SCHEDULED",
      "read": false,
      "read_at": null,
      "created_at": "2026-10-25T21:14:03.512Z",

      "recipient_agent_id": "5b0e…",
      "recipient_name": "Laura Gómez",

      "actor_agent_id": "a91c…",
      "actor_name": "Asistente Ramírez",
      "actor_role": "AI_AGENT",

      "lead_id": "c3d2…",
      "client_name": "Juan Pérez",
      "listing_address": "Cra 43A #10-25",
      "neighborhood": "El Poblado",

      "assigned_via": null,
      "from_agent_id": null,

      "appointment_id": "77fa…",
      "visit_scheduled_at": "2026-10-30T15:00:00Z",
      "visit_duration_min": 60,
      "visit_status": "PENDING_CONFIRMATION",
      "booked_status": "PENDING_CONFIRMATION"
    }
  ],
  "unread_count": 3,
  "next_before": null
}
```

| Field | Meaning |
|---|---|
| `id` | Message id. Use it to mark the message read. Higher = newer. |
| `type` | `LEAD_ASSIGNED` or `VISIT_SCHEDULED` |
| `read` / `read_at` | **The read flag.** `false` → draw it as unread. `read_at` = when it was opened. |
| `created_at` | When it happened |
| `recipient_*` | Who received it (always you in your own inbox; useful in the monitor) |
| `actor_*` | Who caused it. `actor_role` is `AI_AGENT` (the Telegram bot) or `TEAM_ADMIN` |
| `lead_id`, `client_name`, `listing_address`, `neighborhood` | The lead it is about. Link to the lead page. |
| `assigned_via` | `LEAD_ASSIGNED` only: `CREATED` (the bot or an admin opened it for you) or `REASSIGNED` (an admin moved it to you) |
| `from_agent_id` | `REASSIGNED` only: who had it before |
| `appointment_id` | `VISIT_SCHEDULED` only. Link to the visit. |
| `visit_scheduled_at`, `visit_duration_min`, `visit_status` | The visit **as it is now**. It may have been confirmed, moved or cancelled since the message. |
| `booked_status` | What the visit was booked as. `PENDING_CONFIRMATION` = it needed the agent's confirmation; `CONFIRMED` = the bot booked it directly. |

Fields that don't apply to a type are `null`.

Page fields:

| Field | Meaning |
|---|---|
| `unread_count` | Unread messages matching your filters, across **all** pages. With no filters on `/me/notifications`: **the bell badge**. |
| `next_before` | Pass as `?before=` to load the next (older) page. `null` = no more pages. |

---

## 3. Agent: bell and inbox

### Load the inbox — `GET /me/notifications`

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/me/notifications"
```

Returns the **latest 20**, newest first.

| Query param | Use |
|---|---|
| `type` | `LEAD_ASSIGNED` or `VISIT_SCHEDULED`: tabs or chips |
| `read` | `false` = unread only, `true` = read only, omit = all |
| `before` | `next_before` from the previous page: "Load older" button |
| `limit` | 1–20 (default 20). Above 20 → **422**. |

**Loading the list never marks anything read.**

### Mark one read — `POST /me/notifications/{id}/read` → `204`

Call it when the agent **opens/clicks** a message. Calling it twice is harmless
(`read_at` keeps the first time). **404** if the id isn't the caller's message.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" "$BASE/me/notifications/1287/read"
```

### Mark all read — `POST /me/notifications/read-all`

```json
{ "marked": 3 }
```

### UI rules

- **Badge** = `unread_count` from an **unfiltered** `GET /me/notifications`. Hide
  it at 0.
- **Unread** (`read: false`): bold text, a dot or highlighted background.
  **Read**: normal text, no dot.
- **Clicking a message:** call `/read`, update it locally to `read: true` and
  decrement the badge (no need to refetch), then navigate:
  - `LEAD_ASSIGNED` → the lead page (`lead_id`)
  - `VISIT_SCHEDULED` → the visit / calendar (`appointment_id`, `visit_scheduled_at`)
- **"Mark all as read"** button → `/read-all`, set everything read locally and
  the badge to 0.
- **Polling:** every **60 s** while the tab is visible. Pause on
  `document.visibilitychange` → hidden, and refetch immediately when it comes
  back. Also refetch after the user acts on a lead or visit.
- **New-message toast (optional):** remember the highest `id` you've shown. If a
  poll returns a bigger one, it's new.

### Suggested message text

The API sends facts; the frontend writes the sentence. Use "the assistant" (or
the bot's `actor_name`) when `actor_role` is `AI_AGENT`, and `actor_name` for
admins.

| Case | English | Español |
|---|---|---|
| `LEAD_ASSIGNED` + `CREATED` | New lead from {actor}: {client_name}, {listing_address} | Nuevo lead de {actor}: {client_name}, {listing_address} |
| `LEAD_ASSIGNED` + `REASSIGNED` | {actor} assigned you a lead: {client_name}, {listing_address} | {actor} te asignó un lead: {client_name}, {listing_address} |
| `VISIT_SCHEDULED`, `visit_status` = `PENDING_CONFIRMATION` | {actor} booked a visit with {client_name} on {date time}: **needs your confirmation** | {actor} agendó una visita con {client_name} el {fecha hora}: **requiere tu confirmación** |
| `VISIT_SCHEDULED`, `visit_status` = `CONFIRMED` | {actor} booked a visit with {client_name} on {date time} | {actor} agendó una visita con {client_name} el {fecha hora} |
| `VISIT_SCHEDULED`, `visit_status` = `CANCELLED` | add a muted "cancelled" tag | etiqueta "cancelada" |

Use `visit_status`, not `booked_status`, to decide whether to show "needs your
confirmation". It goes away once the agent confirms. Use `visit_scheduled_at`
for the date, since the visit may have moved.

---

## 4. Admin: the monitor

### `GET /notifications` — `TEAM_ADMIN` only

Every message in the admin's agency: what was sent to whom, by whom, and whether
they've read it. Same item shape, newest first, 20 per page.

| Query param | Use |
|---|---|
| `recipient_agent_id` | Messages one agent received. Agent ids from `GET /agents`. |
| `actor_agent_id` | Messages one actor caused |
| `by_bot` | `true` = caused by the AI agent, `false` = by an admin |
| `type` | `LEAD_ASSIGNED` / `VISIT_SCHEDULED` |
| `read` | `false` = still unread: "who hasn't looked yet" |
| `lead_id` | Everything about one lead |
| `created_from`, `created_to` | Inclusive days, `YYYY-MM-DD`, Bogotá time. `from > to` → **422** |
| `before`, `limit` | Paging, as in the inbox (max 20) |

```bash
# unread visit messages the bot sent this week
curl -s -H "Authorization: Bearer $TOKEN" \
  "$BASE/notifications?type=VISIT_SCHEDULED&by_bot=true&read=false&created_from=2026-10-20"
```

- **Viewing the monitor never marks anything read.** The read flag belongs to
  the recipient. An admin's own messages are marked read from their own inbox.
- **Non-admins get 403**: hide the monitor link unless `GET /me` returns
  `role: "TEAM_ADMIN"`.
- `unread_count` here = unread messages matching the filters (all pages). Good
  for a header like "12 unread".

### Suggested monitor table

| Column | From |
|---|---|
| When | `created_at` |
| To | `recipient_name` |
| What | `type` (+ `assigned_via`) |
| Lead | `client_name`, `listing_address` → link `lead_id` |
| By | `actor_name` + a "bot" tag if `actor_role = AI_AGENT` |
| Read | ✓ + `read_at`, or "unread for 3 h" (now − `created_at`) |

Filters bar: agent (recipient), type, bot/admin, read status, date range.

---

## 5. Errors

| Code | When |
|---|---|
| 401 | Missing or expired token |
| 403 | `/notifications` as a non-admin, or `/read` as the bot |
| 404 | `/me/notifications/{id}/read` for a message that isn't yours |
| 422 | `limit` > 20, bad enum value, `created_from` after `created_to` |

---

## 6. TypeScript types

```ts
export type NotificationType = "LEAD_ASSIGNED" | "VISIT_SCHEDULED";
export type AppointmentStatus =
  | "PENDING_CONFIRMATION" | "CONFIRMED" | "RESCHEDULED"
  | "CANCELLED" | "COMPLETED" | "NO_SHOW";

export interface Notification {
  id: number;
  type: NotificationType;
  read: boolean;
  read_at: string | null;
  created_at: string;
  recipient_agent_id: string;
  recipient_name: string | null;
  actor_agent_id: string | null;
  actor_name: string | null;
  actor_role: "AGENT" | "TEAM_ADMIN" | "AI_AGENT" | null;
  lead_id: string;
  client_name: string | null;
  listing_address: string | null;
  neighborhood: string | null;
  assigned_via: "CREATED" | "REASSIGNED" | null;
  from_agent_id: string | null;
  appointment_id: string | null;
  visit_scheduled_at: string | null;
  visit_duration_min: number | null;
  visit_status: AppointmentStatus | null;
  booked_status: AppointmentStatus | null;
}

export interface NotificationPage {
  items: Notification[];
  unread_count: number;
  next_before: number | null;
}
```

Minimal polling hook logic:

```ts
async function poll() {
  if (document.hidden) return;
  const page: NotificationPage = await api.get("/me/notifications");
  setItems(page.items);
  setBadge(page.unread_count);
}
setInterval(poll, 60_000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
```

---

## 7. What is *not* included (yet)

- Messages for rescheduled or cancelled visits, or for the previous owner of a
  reassigned lead. A cancelled visit still shows through `visit_status`.
- Admins choosing which message types get sent.
- Email, push, or live (websocket) delivery.
- A cross-agency support view.

The interactive reference is at `$BASE/docs` (Swagger). Look under the
**notifications** tag.

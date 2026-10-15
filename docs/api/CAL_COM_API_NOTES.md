# Cal.com API v2 — Integration Notes

> Learned 2026-07-25, updated 2026-07-30 with Firecrawl-scraped API docs.
> Full API index: https://cal.com/docs/llms.txt
> OpenAPI spec: https://cal.com/docs/api-reference/v2/openapi.json

---

## Auth

API key. Created at Cal.com → Settings → Security.

```
Authorization: Bearer cal_live_<key>
```

Keys in use:
- `cal_live_73a8b37fb9d39ca19e49ebee5916e841` — **Dialux SDR** (Jay), event `3801235` diallux_live 45-min
- `cal_live_be2771c023e537f625c81119177af025` — Akrit, event `6522694` Diallux Booking Demo 60-min

**Cloudflare gate:** raw HTTP clients without a `User-Agent` header get **403 error 1010**. Always send e.g. `"User-Agent": "Mozilla/5.0 ... diallux-maintenance/1.0"`. (Verified 2026-08-24; supersedes the old "urllib broken" note.)

Works for:
- GET event types
- GET/POST bookings (list, create, cancel)
- GET slots/availability

Test mode keys: `cal_` prefix
Live mode keys: `cal_live_` prefix
Rate limit: 120 req/min (can be increased)

---

## `cal-api-version` Header

**Critical:** Each endpoint requires its own API version in the header.
No header or wrong version → 404.

| Endpoint | Required `cal-api-version` |
|----------|---------------------------|
| `GET /v2/slots` | `2024-09-04` |
| `POST /v2/bookings` | **`2026-05-01`** (was wrongly documented as `2024-09-04` — that returns 404) |
| `GET /v2/bookings` | `2026-05-01` |
| `POST /v2/bookings/{uid}/cancel` | `2026-02-25` — **body REQUIRED** |
| `GET /v2/event-types/{id}` | `2024-06-11` |
| `GET /v2/schedules` | `2024-09-04` |

When in doubt, try `2024-09-04` first, then fall back to other versions.

> **Verified 2026-08-01:** Creating a booking requires `cal-api-version: 2026-05-01` (HTTP 201). Cancel requires `2026-02-25` (HTTP 200). Slot reservation requires `slotStart` (not `slotStartTime`).

---

## Available Slots

```
GET https://api.cal.com/v2/slots
  ?eventTypeId=6389911
  &start=2026-07-30T00:00:00.000Z
  &end=2026-08-10T23:59:00.000Z
  &duration=30
Headers:
  Authorization: Bearer cal_live_<key>
  cal-api-version: 2024-09-04
```

**Params:**
| Param | Type | Description |
|-------|------|-------------|
| `eventTypeId` | int | Required. Event type ID |
| `start` | ISO 8601 | Start of range (NOT `startTime`) |
| `end` | ISO 8601 | End of range (NOT `endTime`) |
| `duration` | int | Slot duration in minutes |

**Response:**
```json
{
  "status": "success",
  "data": {
    "2026-07-30": [
      { "start": "2026-07-30T16:30:00.000Z" }
    ]
  }
}
```

---

## List Bookings (Get All)

```
GET https://api.cal.com/v2/bookings
  ?status=upcoming
  &eventTypeId=6389911
  &limit=100
  &cursor=<pagination_token>
Headers:
  Authorization: Bearer cal_live_<key>
  cal-api-version: 2026-05-01
```

**Params:**
| Param | Type | Description |
|-------|------|-------------|
| `status` | string | `upcoming`, `past`, `cancelled`, `recurring`, `all` |
| `eventTypeId` | int | Filter by event type |
| `limit` | int | Max items (default 50) |
| `cursor` | string | Pagination cursor from `pagination.nextCursor` |

**Response:** Returns array of bookings with full attendee, host, and event type info.

---

## Get a Single Booking

```
GET https://api.cal.com/v2/bookings/{bookingUid}
Headers:
  Authorization: Bearer cal_live_<key>
  cal-api-version: 2026-05-01   ← verified 2026-08-24; `2024-09-04` returns 404
```

---

## Cancel a Booking

```
POST https://api.cal.com/v2/bookings/{bookingUid}/cancel
Headers:
  Authorization: Bearer cal_live_<key>
  Content-Type: application/json
  cal-api-version: 2026-02-25
Body:
  {"cancellationReason": "reason text", "cancelSubsequentBookings": false}
```

> **Verified 2026-08-24 (26/26 cancelled):**
> - `cancellationReason` is REQUIRED. Empty/no body → 400 `"Cancellation reason is required"`.
> - Key must be exactly `cancellationReason` — `{"reason": ...}` → 400 `"property reason should not exist"`. This single gotcha broke every cancel attempt in the project until 2026-08-24.
> - Success = HTTP 200 `{"status":"success","data":{...}}`.

`cancelSubsequentBookings: true` only for recurring bookings.

---

## Create a Booking

```
POST https://api.cal.com/v2/bookings
Headers:
  Authorization: Bearer cal_live_<key>
  Content-Type: application/json
  cal-api-version: 2026-05-01   ← NOT 2024-09-04 (404); see version note above
Body:
  {
    "eventTypeId": 6389911,
    "start": "2026-07-30T16:30:00.000Z",
    "attendee": {
      "name": "John",
      "email": "jaydiallux@gmail.com",
      "timeZone": "Europe/London",
      "phoneNumber": "+441234567890"
    },
    "meetingUrl": "google:meet",
    "guests": [],
    "location": "integration",
    "bookingFieldsResponses": {
      "notes": "...",
      "title": "..."
    }
  }
```

---

## Reserve a Slot (Hold Temporarily)

```
POST https://api.cal.com/v2/slots/reservations
Headers:
  Authorization: Bearer cal_live_<key>
  Content-Type: application/json
  cal-api-version: 2024-09-04
Body:
  {
    "eventTypeId": 6389911,
    "slotStart": "2026-07-30T16:30:00.000Z",   ← verified key (NOT slotStartTime)
    "reservationDuration": 300
  }
```

Reserves a slot for `reservationDuration` seconds (default 300 = 5 min).
DELETE to release: `DELETE /v2/slots/reservations/{reservationUid}`

---

## Our Event Type

> **Note:** Three event types exist. The Dialux SDR live-call event (Jay's, all chat-agent tests book here):

| Field | Value |
|-------|-------|
| ID | `3801235` |
| Title | `Dialux Live Demo` |
| Account | Jay (jaydiallux@gmail.com), API key `cal_live_73a8b37f...` |
| Length | 45 min |
| Location | phone call / video |
| Notes | attendee email always jaydiallux@gmail.com (shared system attendee); attendeePhoneNumber = prospect E.164 |

---

> Second event type: The production/webhook event (Akrit's) is below. The legacy event `6389911` (HVAC_UK_appt, Google Meet) is superseded.

| Field | Value |
|-------|-------|
| ID | `6522694` |
| Title | `Diallux Booking Demo` |
| Slug | `diallux-booking-demo` |
| Booking URL | `https://cal.com/akrit-pun-3qh9xk/diallux-booking-demo` |
| Owner | Akrit Pun (user ID 3012042), API key `cal_live_a9704f08...` (Akrit's) |
| Length | 60 min |
| Timezone | Europe/London (owner) |
| Location | `somewhereElse` → custom label "Address" (NOT Google Meet) |
| minimumBookingNotice | 120 min |

### Legacy event (superseded)

| Field | Value |
|-------|-------|
| ID | `6389911` |
| Title | `HVAC_UK_appt` |
| Slug | `hvac-uk-appt` |
| Owner | Jay at Diallux (jaydiallux@gmail.com, user ID 1833776) |
| Length | 30 min |
| Timezone | Europe/London (booker) / America/Mexico_City (owner) |
| Schedule ID | 965247 |
| Location | Google Meet |

---

## Full Docs Index

All v2 endpoints (from Firecrawl scrape of https://cal.com/docs/llms.txt):

**Bookings:**
- `GET /v2/bookings` — list all (cal-api-version: 2026-05-01)
- `GET /v2/bookings/{uid}` — get one (**2026-05-01**, verified; 2024-09-04 → 404)
- `POST /v2/bookings` — create (**2026-05-01**)
- `POST /v2/bookings/{uid}/cancel` — cancel (2026-02-25)
- `POST /v2/bookings/{uid}/confirm` — confirm (2024-06-11)
- `POST /v2/bookings/{uid}/decline` — decline (2024-06-11)
- `GET /v2/bookings/{uid}/attendees` — list attendees
- `POST /v2/bookings/{uid}/attendees` — add attendee
- `POST /v2/bookings/{uid}/guests` — add guests
- `GET /v2/bookings/{uid}/recordings` — get recordings
- `GET /v2/bookings/{uid}/calendar-links` — get add-to-cal links
- `GET /v2/bookings/routing-trace/{uid}` — routing trace

**Slots:**
- `GET /v2/slots` — get available time slots (2024-09-04)
- `POST /v2/slots/reservations` — reserve a slot (2024-09-04)
- `GET /v2/slots/reservations/{uid}` — get reserved slot
- `DELETE /v2/slots/reservations/{uid}` — delete reserved slot
- `PUT /v2/slots/reservations/{uid}` — update reserved slot

**Schedules:**
- `GET /v2/schedules` — list all
- `POST /v2/schedules` — create
- `GET /v2/schedules/{id}` — get one
- `PUT /v2/schedules/{id}` — update
- `DELETE /v2/schedules/{id}` — delete
- `GET /v2/schedules/default` — get default schedule

**Event Types (individual):**
- `GET /v2/event-types?username={user}` — list by user
- `GET /v2/event-types/{id}` — get one

**Teams / Orgs event types:**
- `GET /v2/teams/event-types` — list team event types
- `POST /v2/teams/event-types` — create
- `GET /v2/teams/event-types/{id}` — get one
- `PUT /v2/teams/event-types/{id}` — update
- `DELETE /v2/teams/event-types/{id}` — delete

**Webhooks:**
- `GET /v2/webhooks` — list all
- `POST /v2/webhooks` — create
- `GET /v2/webhooks/{id}` — get one
- `PUT /v2/webhooks/{id}` — update
- `DELETE /v2/webhooks/{id}` — delete

**Managed Users / Platform (deprecated):**
- `POST /v2/platform/managed-users` — create managed user
- OAuth client credentials required (`x-cal-client-id` + `x-cal-secret-key`)

**Organizations (requires org admin role):**
- Users, teams, schedules, event types, webhooks, roles, routing forms, workflows, OOO entries, conferencing, Stripe, verified resources

---

## Known Issues

- `cal-api-version` header value differs per endpoint — no universal version
- The platform API key works for most v2 endpoints, but some (managed users, org admin) require OAuth client credentials
- Python `urllib` → 403 on cancel (suspect content-length/encoding issue). Use `curl` or `requests` instead.
- Retell's `book_appointment_cal` tool embeds the Cal.com API key + event type ID in the LLM tool definition. Retell makes the Cal.com API call internally — you don't need to call Cal.com directly for booking.

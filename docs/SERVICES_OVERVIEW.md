# Services Overview

Two FastAPI services are deployed on the Hetzner VPS behind Caddy, both under the existing `slots.diallux-ai.site` domain.

## Time Service (agent-agnostic time parsing)

- **Purpose:** Parse Retell's dynamic time values (`{{current_time_Europe/London}}`) and return standardized formats (today, now, timestamp, iso_format) for any agent
- **Public URL:** `https://slots.diallux-ai.site/time-function/*` (Caddy strips the `/time-function` prefix → `127.0.0.1:8002`)
  - `POST /time-function/parse-time` (signed — `X-Retell-Signature`)
  - `GET /time-function/today` (no signature; mirrors cal_slots_endpoint `/today`)
- **Systemd user service:** `time-service.service`
- **Port:** 8002 (localhost only)
- **Source:** `time_endpoint/`
- **Docs:** `time_endpoint/README.md`, `time_endpoint/ARCHITECTURE.md`

## Cal.com Slot Service (slot availability)

- **Purpose:** Cal.com slot availability checking (custom function tool for Retell agents)
- **Public URL:** `https://slots.diallux-ai.site/check_availability`
- **Systemd user service:** `cal-slots.service`
- **Port:** 8001 (localhost only)
- **Source:** `cal_slots_endpoint/`
- **Docs:** `cal_slots_endpoint/README.md`, `cal_slots_endpoint/ARCHITECTURE.md`
- **`check_availability` args:** `slot_target_date` (YYYY-MM-DD), optional `account_id`,
  optional `timezone` (IANA) to return `day`/`time` in that zone (default = account timezone).
  `time` is bare local (no Z/ms) and must be booked paired with the SAME `timezone`. Per-account
  `include_iso` (default true) strips `iso` when set false (e.g. `diallux`).

## Architecture

```
slots.diallux-ai.site (Caddy)
├── /time-function*  → uri strip_prefix /time-function → 127.0.0.1:8002 (time_service)
└── everything else  → 127.0.0.1:8001 (cal_slots)

VPS: Hetzner, user 2nd_workspace
Services: systemd --user time-service + cal-slots.service
```

## Notes

- **Migration:** cal_slots_endpoint `/today` stays for backward compatibility; new agents/tests should use the time service.
- **Deployment:** `time_endpoint/deploy.sh` (rsync + systemd install), then update the Caddy site block (see `time_endpoint/README.md`).
- **Restart rules:** never restart `cal-slots.service` when deploying the time service; Caddy reloads are non-disruptive.
